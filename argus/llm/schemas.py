"""What an agent is allowed to say (LLM_AGENTS.md §2.1).

Every LLM answer is parsed into one of these models, and the validators encode the trading rules
that must hold whatever the model produces. This is the second of three guardrails: the prompt
asks for good output, these types refuse bad output, and the Risk Gate — which is code, not
prompts — decides what actually happens.

The most important property is what is *absent*. There is no `size` field and no `leverage`
field, so no prompt injection, hallucination or clever phrasing can make a model choose how much
money to risk. That decision lives in `argus/gate/sizing.py` and nowhere else.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus.gate.killswitch import RuleKind

Side = Literal["long", "short"]
RegimeLabel = Literal["TREND_UP", "TREND_DOWN", "CHOP", "EVENT_RISK"]
MAX_TIME_STOP_HOURS = 168  # one week: beyond that it is no longer a swing trade
Score = Annotated[int, Field(ge=-2, le=2)]

# Bounds on what a model may write in free text (L-10, non-negotiable 6). Generous on purpose:
# they stop a runaway or injected answer from being journaled and read back into later prompts
# (the Reflector reads theses), not a long honest explanation. No threshold, size or risk number
# lives in any of them.
Prose = Annotated[str, Field(max_length=2000)]  # a thesis
Note = Annotated[str, Field(max_length=1000)]  # a reason, a rationale, a note
MAX_CITATION_CHARS = 160
MAX_FREE_TEXT_ITEMS = 20  # entries in a free-text list (objections, data gaps)
MAX_CITATIONS_PER_FACTOR = 8
MAX_EVIDENCE_KEYS = 10  # five factors, with room for a model that names a sub-factor
MAX_INVALIDATION_RULES = 5  # one per rule kind


def _finite_positive(value: float, field: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be a finite positive price, got {value!r}")
    return value


class Strict(BaseModel):
    """Unknown fields are a refusal, not a shrug.

    A model that invents `"leverage": 20` should fail loudly rather than have it silently
    dropped — the drop would look like agreement.
    """

    model_config = ConfigDict(extra="forbid")


class FactorScores(Strict):
    fundamental: Score
    onchain: Score
    macro: Score
    catalyst: Score
    technical: Score

    @property
    def total(self) -> int:
        return self.fundamental + self.onchain + self.macro + self.catalyst + self.technical

    def non_zero(self) -> list[str]:
        return [name for name in type(self).model_fields if getattr(self, name) != 0]


class Target(Strict):
    price: float
    close_pct: Annotated[int, Field(ge=1, le=100)]

    @model_validator(mode="after")
    def _check(self) -> Target:
        _finite_positive(self.price, "target price")
        return self


class InvalidationRule(Strict):
    """One machine-checkable invalidation, in the closed set `gate/killswitch.parse_rule` knows.

    Was `dict[str, object]`: anything a model wrote crossed into the journal and the gate's input
    unparsed (L-10). A rule that cannot be evaluated is no rule (F-013), so the shape is refused
    here, where the model can still repair it, rather than discovered when it is needed.
    """

    kind: RuleKind
    level: float | None = None
    sign: Literal["positive", "negative"] | None = None
    event: Annotated[str, Field(max_length=80)] | None = None
    outcome: Annotated[str, Field(max_length=80)] | None = None
    timeframe: Literal["1h", "4h", "1d", "1w"] = "1d"

    @model_validator(mode="after")
    def _check(self) -> InvalidationRule:
        if self.kind in ("price_close_above", "price_close_below"):
            if self.level is None:
                raise ValueError(f"{self.kind} needs a level")
            _finite_positive(self.level, f"{self.kind} level")
        if self.kind == "real_yield_above":
            # A real yield can be negative; it only has to be a number.
            if self.level is None or not math.isfinite(self.level):
                raise ValueError("real_yield_above needs a finite level")
        if self.kind == "funding_flip" and self.sign is None:
            raise ValueError("funding_flip needs a sign")
        if self.kind == "event" and not (self.event or "").strip():
            raise ValueError("an event rule must name the event")
        if self.kind == "event" and not (self.outcome or "").strip():
            # The kill switch fires an event rule only when the event resolves to this outcome;
            # without one the rule can never fire, which is no rule (F-013).
            raise ValueError("an event rule must name the outcome that invalidates the trade")
        return self


class TradePlan(Strict):
    """The Trader's proposal. Sizing and leverage are deliberately not expressible here."""

    symbol: str
    side: Side
    entry_kind: Literal["limit", "market"]
    entry_low: float
    entry_high: float
    stop: float
    targets: list[Target] = Field(min_length=1)
    time_stop_hours: Annotated[int, Field(ge=1, le=MAX_TIME_STOP_HOURS)]
    scores: FactorScores
    # factor name -> dossier field paths that justify it. Bounded in `_check`, not in the type:
    # `maxProperties`/`propertyNames` are JSON Schema keywords no provider here has been seen to
    # accept in a tool definition, and a rejected schema is a dead role, not a safer one.
    evidence: dict[str, list[Annotated[str, Field(max_length=MAX_CITATION_CHARS)]]] = Field(
        default_factory=dict
    )
    thesis: Prose = ""
    invalidation_rules: Annotated[
        list[InvalidationRule], Field(max_length=MAX_INVALIDATION_RULES)
    ] = Field(default_factory=list)
    event_thesis: bool = False
    urgent: bool = False

    @model_validator(mode="after")
    def _check(self) -> TradePlan:
        for name in ("entry_low", "entry_high", "stop"):
            _finite_positive(getattr(self, name), name)
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must not exceed entry_high")

        total = sum(t.close_pct for t in self.targets)
        if total != 100:
            raise ValueError(f"targets must close 100% of the position, got {total}%")

        long = self.side == "long"
        # A stop on the wrong side of the entry is an instant loss the exchange would reject.
        if long and self.stop >= self.entry_low:
            raise ValueError("a long stop must sit below the entry band")
        if not long and self.stop <= self.entry_high:
            raise ValueError("a short stop must sit above the entry band")

        reference = self.entry_high if long else self.entry_low
        prices = [t.price for t in self.targets]
        if any((p <= reference) if long else (p >= reference) for p in prices):
            raise ValueError("every target must lie in the profit direction from the entry")
        if prices != sorted(prices, reverse=not long):
            raise ValueError("targets must be in order, nearest to the entry first")

        # A score with no citation is an opinion; the gate is not allowed to price opinions.
        missing = [name for name in self.scores.non_zero() if not self.evidence.get(name)]
        if missing:
            raise ValueError(f"no evidence cited for non-zero scores: {', '.join(missing)}")
        if len(self.evidence) > MAX_EVIDENCE_KEYS:
            raise ValueError(f"evidence names at most {MAX_EVIDENCE_KEYS} factors")
        if any(len(key) > 40 for key in self.evidence):
            raise ValueError("an evidence key is a factor name, not prose")
        if any(len(paths) > MAX_CITATIONS_PER_FACTOR for paths in self.evidence.values()):
            raise ValueError(f"at most {MAX_CITATIONS_PER_FACTOR} citations per factor")
        return self


class NoTrade(Strict):
    """A first-class answer (F-001): "nothing here" must be as easy to say as a trade."""

    symbol: str
    reason: Note


class RegimeCall(Strict):
    """The run's weather report: what kind of market this is, and how brave to be in it.

    `posture` has no value that increases risk. The deterministic classifier in
    `features/regime.py` sets the label and the multipliers; this agent may only ask for *less*
    than that, never more. Making "increase" inexpressible is the same protection as having no
    size field on a TradePlan — a model cannot argue for something it has no way to say.
    """

    label: RegimeLabel
    posture: Literal["normal", "reduce", "stand_aside"] = "normal"
    confidence: Annotated[int, Field(ge=0, le=5)]
    reasons: list[str] = Field(min_length=1)
    top_risks: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _check(self) -> RegimeCall:
        if self.posture == "stand_aside" and self.confidence < 2:
            # Standing the whole book aside is the most expensive thing this agent can ask for.
            # Asking for it while unsure of the read is how a quiet week becomes a dead month.
            raise ValueError("stand_aside needs confidence >= 2")
        return self


class RedTeamVerdict(Strict):
    agree: bool
    severity: Annotated[int, Field(ge=0, le=5)]
    objections: Annotated[list[Note], Field(min_length=1, max_length=MAX_FREE_TEXT_ITEMS)]


class ReviewDecision(Strict):
    """The Reviewer's verdict on an open position.

    `new_stop` may only move towards profit. A reviewer that can widen a stop is a reviewer that
    can turn a loss into a bigger one, and no amount of rationale should buy that.
    """

    action: Literal["HOLD", "CLOSE", "TRIM", "MOVE_STOP"]
    side: Side
    rationale: Note
    current_stop: float | None = None
    new_stop: float | None = None
    trim_pct: Annotated[int, Field(ge=1, le=100)] | None = None

    @model_validator(mode="after")
    def _check(self) -> ReviewDecision:
        if self.action == "MOVE_STOP":
            if self.new_stop is None or self.current_stop is None:
                raise ValueError("MOVE_STOP needs both current_stop and new_stop")
            _finite_positive(self.new_stop, "new_stop")
            tighter = (
                self.new_stop > self.current_stop
                if self.side == "long"
                else self.new_stop < self.current_stop
            )
            if not tighter:
                raise ValueError("a stop may only be moved towards profit, never widened")
        if self.action == "TRIM" and self.trim_pct is None:
            raise ValueError("TRIM needs trim_pct")
        return self


class TraderAnswer(Strict):
    """The Trader's whole answer in one shape.

    Deliberately not `TradePlan | NoTrade`. A union makes pydantic-ai expose one output tool per
    member, and several free models cannot choose between two tools: Groq's gpt-oss-120b wrote a
    well-reasoned refusal on 2026-09-20 and then called a tool named `json` that was never
    offered, so a good answer was lost to a protocol mismatch (F-087). One tool with a
    discriminator is understood by every model we route to.
    """

    decision: Literal["trade", "no_trade"]
    plan: TradePlan | None = None
    reason: Note | None = None

    @model_validator(mode="after")
    def _check(self) -> TraderAnswer:
        if self.decision == "trade" and self.plan is None:
            raise ValueError("decision=trade needs a plan")
        if self.decision == "no_trade":
            if not self.reason:
                raise ValueError("decision=no_trade needs a reason")
            if self.plan is not None:
                raise ValueError("decision=no_trade must not carry a plan")
        return self


class AnalystView(Strict):
    """One asset, read family by family, before anyone proposes a trade.

    The Analyst's output is evidence for the Trader, not a decision — it names no entry, no stop
    and no direction. Separating "what is true about this asset" from "what should we do about
    it" is what lets the Red Team attack the reading without attacking the trade.
    """

    symbol: str
    scores: FactorScores
    evidence: dict[str, list[Annotated[str, Field(max_length=MAX_CITATION_CHARS)]]] = Field(
        default_factory=dict
    )
    key_levels: dict[str, float] = Field(default_factory=dict)
    summary: Prose = ""
    confidence: Annotated[int, Field(ge=0, le=5)] = 0
    data_gaps: Annotated[list[Note], Field(max_length=MAX_FREE_TEXT_ITEMS)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _check(self) -> AnalystView:
        missing = [name for name in self.scores.non_zero() if not self.evidence.get(name)]
        if missing:
            raise ValueError(f"no evidence cited for non-zero scores: {', '.join(missing)}")
        for name, value in self.key_levels.items():
            _finite_positive(value, f"key_levels.{name}")
        return self


class RiskVerdict(Strict):
    """Is the RISK of this trade sensible, given everything else in the book?

    Not whether the idea is good — the Analyst and Red Team argue that. This asks whether taking
    it *now*, at this size, alongside what is already open, is prudent. Like `RegimeCall` it has
    no way to ask for more: `proceed` means "as the gate sized it", never "bigger".
    """

    recommendation: Literal["proceed", "reduce", "decline"]
    concerns: list[str] = Field(default_factory=list)
    rationale: Note

    @model_validator(mode="after")
    def _check(self) -> RiskVerdict:
        if self.recommendation != "proceed" and not self.concerns:
            # A reduction nobody can explain cannot be reviewed, and cannot be learned from.
            raise ValueError("reduce or decline must name at least one concern")
        return self


class CatalystCandidate(Strict):
    """A dated, sourced event someone should look at — never a reason to trade by itself.

    The Catalyst Extractor reads headlines, and headlines are the easiest place in this system to
    hallucinate: an unlock that does not exist, a listing that was a rumour, a date invented to
    make a vague sentence fit a field. So the schema refuses the two shapes that invention takes.
    `when` must be a real ISO date or the honest string `unknown` — no "next month", no "soon",
    nothing that reads as precision without being it. And `source` must name the evidence block
    the claim came from, which lets `filter_sourced()` in the agent drop anything whose source was
    not in the prompt. A candidate is a question for owner over Telegram, not an input to sizing.
    """

    symbol: str
    headline: Annotated[str, Field(max_length=160)]
    kind: Literal[
        "unlock", "listing", "upgrade", "governance", "macro", "regulatory", "partnership", "other"
    ]
    when: str
    source: str
    impact: Literal["high", "medium", "low"]

    @model_validator(mode="after")
    def _check(self) -> CatalystCandidate:
        if self.when != "unknown":
            try:
                date.fromisoformat(self.when)
            except ValueError:
                raise ValueError(
                    f"when must be an ISO date (YYYY-MM-DD) or 'unknown', got {self.when!r}"
                ) from None
        if not self.source.strip():
            raise ValueError("every candidate must name the evidence source it came from")
        return self


class CatalystList(Strict):
    candidates: Annotated[list[CatalystCandidate], Field(max_length=6)] = Field(
        default_factory=list
    )
    note: str = ""


class PostMortem(Strict):
    """What the trade taught, judged on process rather than on the result.

    The failure mode here is hindsight: calling a sound plan bad because it lost, or a reckless one
    good because it won. Both poison the Reflector that reads these, and a system that learns
    "never take that setup again" from one unlucky loss will end up unable to trade at all.

    So the judgement is split in two. `process_verdict` asks whether the decision was right with
    what was knowable *at entry*; `would_take_again` asks the same question as a commitment. A
    losing trade with a sound process should come back `sound` and `True`, and a winning trade
    taken on nothing should come back `flawed`. That disagreement with the PnL is the signal —
    which also makes the golden cases scoreable, because outcome-following is visible.
    """

    process_verdict: Literal["sound", "flawed"]
    would_take_again: bool
    entry_timing: Literal["early", "good", "late"]
    sizing: Literal["too_small", "right", "too_big"]
    stop_placement: Literal["too_tight", "right", "too_wide"]
    missed_signals: list[str] = Field(default_factory=list)
    do_differently: str
    feedback: str

    @model_validator(mode="after")
    def _check(self) -> PostMortem:
        if self.process_verdict == "flawed" and not self.do_differently.strip():
            # "Flawed" with nothing to change is a complaint, not a lesson.
            raise ValueError("a flawed process must say what to do differently")
        return self


MAX_LESSON_WORDS = 30
# The floor for a pattern to be a pattern. The promotion *ratio* lives in `lessons.py`; only this
# count is enforced here, because two trades can never become a pattern no matter who counts them.
MIN_SUPPORTING = 3


class LessonCandidate(Strict):
    """A pattern the Reflector claims to see across several closed trades.

        This is the only agent whose output outlives its run: a promoted lesson is rendered into the
        Trader's and Analyst's prompts for the next sixty days. That makes it the highest-leverage
        place in the system for a bad idea to take root, so the type is built to keep a lesson
        *auditable* rather than merely plausible.

    Two bars stand in front of a lesson and they belong in different places. The
    **three-supporter floor is here**, in the type: a claim about one or two trades is an
    anecdote, and an anecdote that reaches ten future prompts is how a system talks itself into a
    habit. Making it unconstructible beats asking a prompt not to do it.

    The **ratio bar is code's**, in `lessons.py`, and the model is told not to apply it. A
    candidate with four supporters and two contradictions should be proposed, refused downstream
    and recorded. Naming that threshold in the prompt made the model pre-filter instead,
    suppressing the very candidates whose counter-examples were worth seeing (F-115).

        Cited ids are checked against the trades actually shown (`ground()`), the same way the
        Catalyst Extractor's sources are.

        What is absent matters as much: there is no field for a threshold, a size, a leverage or a
        risk percentage. A lesson can say what to notice; it can never say how much to bet.
    """

    lesson: str
    scope: Literal["factor", "regime", "symbol_class", "execution"]
    supporting_trade_ids: Annotated[list[str], Field(min_length=MIN_SUPPORTING)]
    contradicting_trade_ids: list[str] = Field(default_factory=list)
    confidence_pct: Annotated[int, Field(ge=0, le=100)]

    @property
    def support(self) -> int:
        return len(set(self.supporting_trade_ids))

    @property
    def contradiction(self) -> int:
        return len(set(self.contradicting_trade_ids))

    @model_validator(mode="after")
    def _check(self) -> LessonCandidate:
        if len(self.lesson.split()) > MAX_LESSON_WORDS:
            raise ValueError(f"a lesson must be at most {MAX_LESSON_WORDS} words")
        if len(set(self.supporting_trade_ids)) < MIN_SUPPORTING:
            raise ValueError(
                f"a lesson needs {MIN_SUPPORTING} distinct supporting trades; fewer is an anecdote"
            )
        both = set(self.supporting_trade_ids) & set(self.contradicting_trade_ids)
        if both:
            raise ValueError(f"trades cannot both support and contradict a lesson: {sorted(both)}")
        return self


class LessonSet(Strict):
    candidates: Annotated[list[LessonCandidate], Field(max_length=8)] = Field(default_factory=list)
    note: str = ""


MAX_PLANS_PER_RUN = 2


class TraderBatch(Strict):
    """The Trader's answer when it sees the whole candidate set at once.

    The design always specified one call per run (LLM_AGENTS.md §2); the implementation asked
    per asset because it inherited the shape of the deterministic stub it replaced (F-117). That
    difference is not only cost. Fifteen independent calls cannot rank anything: each one answers
    "is this tradeable?" in isolation, so the gate approves the first plan that passes rather than
    the best one available, and nothing ever notices that three candidates are the same bet.

    Still one tool with one list, not a union — several free models cannot choose between two
    output tools (F-087), and that lesson survives the change.

    The cap is here rather than in a prompt because a prompt is a request and a validator is a
    refusal. `symbol` is checked against the offered set by the caller, since only the caller
    knows what was offered.
    """

    plans: Annotated[list[TradePlan], Field(max_length=MAX_PLANS_PER_RUN)] = Field(
        default_factory=list
    )
    no_trade_reason: Note = ""
    portfolio_note: Note = ""

    @model_validator(mode="after")
    def _check(self) -> TraderBatch:
        symbols = [plan.symbol for plan in self.plans]
        if len(set(symbols)) != len(symbols):
            # Two plans on one symbol is one position with two opinions about it.
            raise ValueError(f"one plan per symbol; got {sorted(symbols)}")
        if not self.plans and not self.no_trade_reason:
            # "Nothing today" is a real answer and a common one, but it has to be an answer.
            raise ValueError("no plans requires a no_trade_reason")
        return self


class TraderBatchOfThree(TraderBatch):
    """`TraderBatch` with room for three plans: the demo exploration cap (Q3, 2026-10-26).

    A subclass rather than a wider shared cap, so the live schema, and with it every live retry on a
    three-plan answer, stays exactly what it was. Chosen by `trader_batch.batch_schema(limits)`.
    """

    plans: Annotated[list[TradePlan], Field(max_length=3)] = Field(default_factory=list)
