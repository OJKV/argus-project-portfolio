"""The Risk Gate — 14 gates in order (DECISION_LOGIC.md §6; ARCHITECTURE.md §7).

This is the layer the LLM cannot reach. It runs on every `TradePlan`, returns
`APPROVED | ADJUSTED | REJECTED` with a reason per gate, and is journalled whole — rejected
plans are the calibration dataset.

Two rules shape the design:
  * gates run **in order** and stop at the first REJECT, so the reason that surfaces is the
    first thing actually wrong with the plan;
  * adjustments multiply, and if they shrink the trade below half the size the plan implied,
    it is rejected instead — a heavily adjusted plan is a different trade than the one scored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from argus.features.regime import RegimeView
from argus.gate.fees import FUNDING_PERIOD_HOURS, FeeRates, expected_costs, fee_viable
from argus.gate.killswitch import (
    KillRule,
    Triggered,
    conflicts_with_plan,
    parse_rule,
    portfolio_kill,
)
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.gate.protections import TradingHistory, check_protections
from argus.gate.sizing import InstrumentSpec, SizingInput, SizingResult, StepPolicy, size_position

Side = Literal["long", "short"]
Verdict = Literal["PASS", "ADJUST", "REJECT"]
Status = Literal["APPROVED", "ADJUSTED", "REJECTED"]

MIN_SCORE = LIVE_CONSERVATIVE.min_score  # 4. The live values live in gate/limits.py
SCORE_MIN, SCORE_MAX = -2, 2  # DECISION_LOGIC §4: every factor scores in -2..+2
MIN_SCORE_COUNTER_REGIME = LIVE_CONSERVATIVE.min_score_counter_regime  # 6
MAX_DERIVATIVES_AGE_MIN = 120.0
NEWS_MISSING_MULT = 0.5
MIN_RR_TP1 = LIVE_CONSERVATIVE.min_rr_tp1  # 1.5
MIN_RR_BLENDED = LIVE_CONSERVATIVE.min_rr_blended  # 2.0
MAX_ENTRY_DISTANCE_PCT = 3.0
MAX_OPEN_RISK_PCT = LIVE_CONSERVATIVE.max_open_risk_pct  # 5.0
MAX_GROSS_EXPOSURE_PCT = 150.0
MAX_POSITIONS = 4
MAX_SAME_SIDE_HIGH_BETA = 2
HIGH_BETA = 1.3
MAX_FUNDING_SHARE_OF_TP1 = 0.25
EVENT_BLACKOUT_HOURS = 6.0
EVENT_RISK_MULT = 0.5
URGENT_MAX_SPREAD_PCT = 0.03
FREE_MARGIN_BUFFER = 0.9
DOWNSIDE_MISS_LIMIT = 0.25
DOWNSIDE_LEVERAGE_CAP = 5
DOWNSIDE_LIQ_BUFFER = 3.0
MIN_SIZE_FRACTION_OF_IMPLIED = 0.5
# Entries are costed at taker (G-10; DECISION_LOGIC §6 gate 13: "taker unless post-only fill
# assumed"). The order builder places plain limits, never post-only, and a marketable limit in a
# trend fills taker. When a post-only flag exists on the order, this becomes per-plan.
ENTRY_IS_MAKER = False
LIQ_BUFFER_MULTIPLE = 2.5  # liquidation distance >= this x stop distance (gate 5)
STOP_ATR_BAND = (1.0, 3.5)  # stop distance in ATR14 multiples (gate 3)
MAX_TIME_STOP_HOURS = 168  # DECISION_LOGIC §10: beyond a week it is not a swing trade


@dataclass(frozen=True)
class FactorScores:
    fundamental: int
    onchain: int
    macro: int
    catalyst: int
    technical: int

    @property
    def total(self) -> int:
        return self.fundamental + self.onchain + self.macro + self.catalyst + self.technical

    @property
    def veto(self) -> bool:
        # `<=`, not `-2 in`: a -3 that slipped past validation must not be the one score that
        # cannot veto (G-6).
        return min(self.values) <= SCORE_MIN

    @property
    def values(self) -> tuple[int, ...]:
        return (self.fundamental, self.onchain, self.macro, self.catalyst, self.technical)


@dataclass(frozen=True)
class Target:
    price: float
    close_pct: int


@dataclass(frozen=True)
class PlanInput:
    symbol: str
    side: Side
    entry_kind: Literal["limit", "market"]
    entry_low: float
    entry_high: float
    stop: float
    targets: list[Target]
    time_stop_hours: int
    scores: FactorScores
    event_thesis: bool = False
    urgent: bool = False
    invalidation_rules: list[dict[str, Any]] = field(default_factory=list)
    # What the adversarial chain decided (`argus/runs/pipeline.py`): 1.0 normally, 0.5 when the
    # Risk Debate asked for less. A FACTOR, never a size — the arithmetic stays in sizing.py, and
    # `SizingInput` clamps this to 1.0 so no agent can ever ask for more than the gate allowed.
    size_factor: float = 1.0
    # JOURNAL ONLY. No gate reads this and a test asserts none ever does. It exists because the
    # Reflector learns from *why* a trade was taken, and `to_plan_input` drops the model's free
    # text so nothing it writes can influence sizing — which left every closed trade reaching the
    # learning loop with no reason attached (F-130). Carried, never consulted.
    thesis: str = ""
    # JOURNAL ONLY, like `thesis`: the model family that wrote the plan, so the Reviewer of the
    # position it opens can be kept off that family (`reviewer: trader`, L-13). No gate reads it.
    trader_family: str = ""

    @property
    def entry(self) -> float:
        return (self.entry_low + self.entry_high) / 2


@dataclass(frozen=True)
class MarketContext:
    # Optional on purpose (review G-N4, G-14): the builder used to write `or 0` / `or 0.02` here,
    # so a missing price became a ZeroDivisionError and a missing spread became a passing one.
    # Unknown is now carried as None and gate 2 rejects it — a default is a guess the gate cannot
    # tell from a measurement.
    mark: float | None
    atr_pct: float | None
    spread_pct: float | None
    funding_8h: float | None
    swing_high_20: float | None
    swing_low_20: float | None
    regime: RegimeView
    spec: InstrumentSpec
    fees: FeeRates
    # Required (review D-2): a default of True is how the cross-venue check went dead — nothing
    # computed it and the gate reported "consensus ok". The builder states what it measured.
    consensus_ok: bool
    derivatives_age_min: float | None = None
    news_present: bool = True
    suspect_sources: list[str] = field(default_factory=list)
    next_event_hours: float | None = None
    in_market_list: bool = True
    beta_btc: float | None = None
    # Dates of confirmed catalysts in this asset's dossier (its own, or market-wide). The only
    # thing that can corroborate an `event_thesis` inside the blackout (review G-N6 / L-2).
    catalyst_dates: tuple[date, ...] = ()
    # Hours between funding settlements on this instrument (G-9); `funding_8h` is per period.
    funding_interval_hours: float | None = FUNDING_PERIOD_HOURS


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    side: Side
    notional_usd: float
    risk_usd: float
    # None when nobody measured it (a position opened by hand). Gate 6 counts an unknown beta as
    # high: correlation it cannot see is still correlation.
    beta_btc: float | None
    # The venue's margin ratio for this position (OKX `mgnRatio`), None for a planned entry or an
    # unreported one. The book's lowest feeds `PortfolioState.margin_ratio` (§9, review G-3).
    margin_ratio: float | None = None


@dataclass(frozen=True)
class PortfolioState:
    equity: float
    free_margin: float
    positions: list[OpenPosition]
    history: TradingHistory
    peak_equity_30d: float
    margin_ratio: float | None = None
    downside_miss_rate: float | None = None
    active_kill_switches: list[Triggered] = field(default_factory=list)
    # How many of `positions` the venue actually holds (the rest are this run's planned entries,
    # which have no margin ratio yet). None = all of them, which is the fail-closed reading.
    venue_positions: int | None = None


@dataclass
class GateReason:
    gate: int
    name: str
    verdict: Verdict
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "name": self.name,
            "verdict": self.verdict,
            "detail": self.detail,
        }


@dataclass
class GateResult:
    status: Status
    reasons: list[GateReason] = field(default_factory=list)
    sized: SizingResult | None = None
    size_mult: float = 1.0
    leverage_cap: int | None = None
    # The plan's invalidation rules after normalisation (G-4): what the journal keeps is the
    # typed form the gate accepted, not the model's raw dicts.
    rules: list[KillRule] = field(default_factory=list)
    # Gate 9 demands a HALT (weekly loss). Carried out of the gate so the caller writes it: the
    # flag used to be dropped here and the halt "lifted by itself" (G-13c).
    halt: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "size_mult": round(self.size_mult, 4),
            "leverage_cap": self.leverage_cap,
            "rules": [vars(rule) for rule in self.rules],
            "halt": self.halt,
            "reasons": [r.as_dict() for r in self.reasons],
            "sized": None if self.sized is None else vars(self.sized),
        }


def _rr(entry: float, stop: float, price: float, side: Side) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    reward = (price - entry) if side == "long" else (entry - price)
    return reward / risk


def _blended_rr(plan: PlanInput, entry: float) -> float:
    return sum(
        _rr(entry, plan.stop, t.price, plan.side) * (t.close_pct / 100) for t in plan.targets
    )


def _tp1_profit(plan: PlanInput, notional: float, entry: float) -> float:
    if not plan.targets:
        return 0.0
    tp1 = plan.targets[0]
    move = abs(tp1.price - entry) / entry
    return notional * move * (tp1.close_pct / 100)


def _market_numbers(market: MarketContext) -> tuple[float, float, float, float] | list[str]:
    """(mark, atr_pct, spread_pct, funding_8h), or the names absent, NaN/inf or impossible.

    The lower bound is per field. A price and a volatility must be positive and a spread cannot be
    negative, but funding is signed: negative funding (shorts pay longs) is a common reading on
    alts, and refusing it rejected every such plan as missing data.
    """
    named: tuple[tuple[str, float | None, float | None, bool], ...] = (
        # (name, value, lower bound or None for signed, bound is exclusive)
        ("mark", market.mark, 0.0, True),
        ("atr_pct", market.atr_pct, 0.0, True),
        ("spread_pct", market.spread_pct, 0.0, False),
        ("funding_8h", market.funding_8h, None, False),
    )
    bad = [
        name
        for name, value, floor, exclusive in named
        if value is None
        or not math.isfinite(value)
        or (floor is not None and (value <= floor if exclusive else value < floor))
    ]
    if bad:
        return bad
    mark, atr_pct, spread_pct, funding_8h = (float(value or 0.0) for _, value, _, _ in named)
    return mark, atr_pct, spread_pct, funding_8h


def _finite(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def plan_problems(plan: PlanInput) -> list[str]:
    """What makes a plan impossible to judge, before any gate reads it (review G-6).

    `PlanInput` is a plain dataclass and the stub proposer never passed the LLM schema, so NaN
    targets, an inverted entry band, close percentages of 150/-50, scores of -3 or +5 and an
    unbounded time stop all reached arithmetic that silently mishandles them (`nan < MIN` is
    False; `sorted([110, nan])` counts as ordered).
    """
    problems: list[str] = []
    if plan.side not in ("long", "short"):
        problems.append(f"side {plan.side!r}")
    if plan.entry_kind not in ("limit", "market"):
        problems.append(f"entry_kind {plan.entry_kind!r}")
    for name in ("entry_low", "entry_high", "stop"):
        value = getattr(plan, name)
        if not _finite(value) or value <= 0:
            problems.append(f"{name} {value!r} is not a positive finite price")
    if _finite(plan.entry_low) and _finite(plan.entry_high) and plan.entry_low > plan.entry_high:
        problems.append(f"entry band inverted: {plan.entry_low} > {plan.entry_high}")
    if not plan.targets:
        problems.append("no targets")
    for target in plan.targets:
        if not _finite(target.price) or target.price <= 0:
            problems.append(f"target {target.price!r} is not a positive finite price")
        if isinstance(target.close_pct, bool) or not 0 < target.close_pct <= 100:
            problems.append(f"target close_pct {target.close_pct!r} outside 1..100")
    if isinstance(plan.time_stop_hours, bool) or not (
        1 <= plan.time_stop_hours <= MAX_TIME_STOP_HOURS
    ):
        problems.append(
            f"time_stop_hours {plan.time_stop_hours!r} outside 1..{MAX_TIME_STOP_HOURS}"
        )
    if any(isinstance(v, bool) or not SCORE_MIN <= v <= SCORE_MAX for v in plan.scores.values):
        problems.append(f"scores {plan.scores.values} outside {SCORE_MIN}..{SCORE_MAX}")
    if not _finite(plan.size_factor):
        problems.append(f"size_factor {plan.size_factor!r} is not finite")
    return problems


def gate_error(exc: BaseException) -> GateResult:
    """The verdict for a plan the gate could not evaluate: REJECTED, with the reason recorded.

    Review G-N4 / G-19: an exception inside `evaluate` escaped the run loop, left the run row
    RUNNING and stopped every later asset from being gated. A gate that cannot decide has decided.
    """
    return GateResult(
        status="REJECTED",
        reasons=[GateReason(0, "gate_error", "REJECT", f"{type(exc).__name__}: {exc}"[:300])],
    )


def evaluate(  # noqa: PLR0911, PLR0912, PLR0915 - one branch per gate, deliberately flat
    plan: PlanInput,
    market: MarketContext,
    portfolio: PortfolioState,
    steps: tuple[StepPolicy, ...],
    now: datetime,
    max_positions: int = MAX_POSITIONS,
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> GateResult:
    result = GateResult(status="APPROVED")
    size_mult = 1.0

    def add(gate: int, name: str, verdict: Verdict, detail: str) -> None:
        result.reasons.append(GateReason(gate, name, verdict, detail))

    def reject(gate: int, name: str, detail: str) -> GateResult:
        add(gate, name, "REJECT", detail)
        result.status = "REJECTED"
        return result

    # --- the plan itself, before any gate reads it (G-6) --------------------------------
    problems = plan_problems(plan)
    if problems:
        return reject(0, "plan", "; ".join(problems))

    # --- gate 1: score ----------------------------------------------------------------
    counter = (plan.side == "long" and market.regime.label == "TREND_DOWN") or (
        plan.side == "short" and market.regime.label == "TREND_UP"
    )
    threshold = limits.min_score_counter_regime if counter else limits.min_score
    if plan.scores.veto:
        return reject(1, "score", f"a factor scored −2 (veto); total {plan.scores.total:+d}")
    if plan.scores.total < threshold:
        return reject(
            1,
            "score",
            f"total {plan.scores.total:+d} < {threshold:+d}"
            + (" (counter-regime)" if counter else ""),
        )
    add(1, "score", "PASS", f"total {plan.scores.total:+d} >= {threshold:+d}")

    # --- gate 2: data quality ---------------------------------------------------------
    numbers = _market_numbers(market)
    if isinstance(numbers, list):
        return reject(2, "data quality", f"market inputs missing or invalid: {', '.join(numbers)}")
    # Unpacked once here so every later gate works on checked numbers, never on Optional.
    mark, atr_pct, spread_pct, funding_8h = numbers
    interval = market.funding_interval_hours
    if interval is None or not math.isfinite(interval) or interval <= 0:
        return reject(2, "data quality", "market inputs missing or invalid: funding interval")
    beta = market.beta_btc
    if beta is None or not math.isfinite(beta):
        # D-10: an unknown beta read as 0 skipped gate 6's same-side high-beta cap entirely.
        return reject(2, "data quality", "market inputs missing or invalid: beta_btc")
    if not market.consensus_ok:
        return reject(
            2,
            "data quality",
            "price consensus failed (mark not inside a >=3-source majority within 1%)",
        )
    if market.suspect_sources:
        return reject(
            2, "data quality", f"suspect sources cited: {', '.join(market.suspect_sources)}"
        )
    age = market.derivatives_age_min
    if age is None or age > MAX_DERIVATIVES_AGE_MIN:
        return reject(
            2,
            "data quality",
            f"derivatives family {'missing' if age is None else f'{age:.0f}m old'}",
        )
    if not market.news_present:
        size_mult *= NEWS_MISSING_MULT
        add(2, "data quality", "ADJUST", f"no news in 48h — size ×{NEWS_MISSING_MULT}")
    else:
        add(2, "data quality", "PASS", f"consensus ok, derivatives {age:.0f}m old")

    # --- gate 3: structure ------------------------------------------------------------
    # The price this plan will actually enter at (review G-N3 / G-2). A limit rests in its band;
    # a market order fills at the mark, wherever the mark has gone since the plan was written —
    # so distance, ATR band, RR and size are all measured from there, and a mark that has left
    # the band means the trade that was scored no longer exists.
    entry = plan.entry
    if plan.entry_kind == "market":
        if not plan.entry_low <= mark <= plan.entry_high:
            return reject(
                3,
                "structure",
                f"market entry at mark {mark} is outside the planned band "
                f"[{plan.entry_low}, {plan.entry_high}]",
            )
        entry = mark
    # A limit rests at the near edge of its band (entry_low for a long). A long band wholly above
    # the mark, or a short band wholly below it, is a marketable limit: it fills now, before the
    # level the plan waited for, and OKX refuses one outside its price limit (51006, 2026-10-29).
    elif plan.side == "long" and plan.entry_low > mark:
        return reject(
            3,
            "structure",
            f"long entry zone [{plan.entry_low}, {plan.entry_high}] is above the "
            f"mark {mark}: a limit there buys now, not at the level",
        )
    elif plan.side == "short" and plan.entry_high < mark:
        return reject(
            3,
            "structure",
            f"short entry zone [{plan.entry_low}, {plan.entry_high}] is below the "
            f"mark {mark}: a limit there sells now, not at the level",
        )
    if plan.side == "long" and not plan.stop < entry:
        return reject(3, "structure", "long stop is not below entry")
    if plan.side == "short" and not plan.stop > entry:
        return reject(3, "structure", "short stop is not above entry")
    stop_dist_pct = abs(entry - plan.stop) / entry * 100
    band_lo, band_hi = STOP_ATR_BAND[0] * atr_pct, STOP_ATR_BAND[1] * atr_pct
    if not band_lo <= stop_dist_pct <= band_hi:
        return reject(
            3,
            "structure",
            f"stop {stop_dist_pct:.2f}% outside [{band_lo:.2f}%, {band_hi:.2f}%] "
            f"(ATR14 {atr_pct:.2f}%)",
        )
    swing = market.swing_low_20 if plan.side == "long" else market.swing_high_20
    if swing is None or not math.isfinite(swing):
        # G-14: a missing swing level used to skip this check — the one thing it exists to ask
        # ("is the stop beyond structure?") cannot be answered, so the answer is no.
        return reject(3, "structure", "no 20-bar swing level to check the stop against")
    beyond = plan.stop < swing if plan.side == "long" else plan.stop > swing
    if not beyond:
        return reject(3, "structure", f"stop {plan.stop} is inside structure ({swing})")
    if sum(t.close_pct for t in plan.targets) != 100:
        return reject(3, "structure", "target close percentages do not sum to 100")
    prices = [t.price for t in plan.targets]
    ordered = (
        prices == sorted(prices) if plan.side == "long" else prices == sorted(prices, reverse=True)
    )
    if not ordered:
        return reject(3, "structure", "targets are not ordered away from entry")
    rr1 = _rr(entry, plan.stop, plan.targets[0].price, plan.side)
    rr_blended = _blended_rr(plan, entry)
    # Written as "not (good enough)" so a NaN can only ever fail (G-6).
    if not (rr1 >= limits.min_rr_tp1 and rr_blended >= limits.min_rr_blended):
        return reject(
            3,
            "structure",
            f"RR(TP1) {rr1:.2f} < {limits.min_rr_tp1} or blended {rr_blended:.2f} "
            f"< {limits.min_rr_blended}",
        )
    entry_distance = abs(entry - mark) / mark * 100
    if plan.entry_kind == "limit" and entry_distance > MAX_ENTRY_DISTANCE_PCT:
        return reject(
            3,
            "structure",
            f"entry zone {entry_distance:.1f}% from price (max {MAX_ENTRY_DISTANCE_PCT}%)",
        )
    add(
        3,
        "structure",
        "PASS",
        f"stop {stop_dist_pct:.2f}%, RR1 {rr1:.2f}, blended {rr_blended:.2f}",
    )

    # --- gate 8 (evaluated before sizing: it can reject or shrink) ---------------------
    hours = market.next_event_hours
    in_blackout = hours is not None and 0 <= hours <= EVENT_BLACKOUT_HOURS
    if in_blackout:
        # `event_thesis` is a bare boolean from the model, never explained in its prompt, so it
        # is set on a whim or by an injected headline (G-N6 / L-2). It is honoured only when the
        # plan scored the catalyst and the dossier has a dated catalyst inside the window.
        window = {now.date(), (now + timedelta(hours=EVENT_BLACKOUT_HOURS)).date()}
        dated = any(d in window for d in market.catalyst_dates)
        if not (plan.event_thesis and plan.scores.catalyst >= 1 and dated):
            missing = (
                "no event thesis"
                if not plan.event_thesis
                else "event thesis without a scored, dated catalyst in the window"
            )
            return reject(8, "events", f"high-impact event in {hours:.1f}h and {missing}")
    # A CHOP view classified on demo terms is not trusted by a live gate: the profile decides.
    limit_only = market.regime.limit_only or (
        market.regime.label == "CHOP" and limits.chop_limit_only
    )
    if limit_only and plan.entry_kind == "market":
        return reject(
            8, "events", f"{market.regime.label} is limit-only; plan asks for a market entry"
        )
    if in_blackout or market.regime.label == "EVENT_RISK":
        # Applied once whichever way the event arrived: a corroborated thesis still trades the
        # event at event-risk size.
        size_mult *= EVENT_RISK_MULT
        why = "event thesis inside the blackout" if in_blackout else "EVENT_RISK regime"
        add(8, "events", "ADJUST", f"{why} — size ×{EVENT_RISK_MULT}")
    else:
        add(8, "events", "PASS", "no event inside the blackout window")

    # --- gate 9: protections ----------------------------------------------------------
    protection = check_protections(plan.symbol, portfolio.history, now, limits)
    result.halt = protection.halt
    if not protection.allowed:
        return reject(9, "protections", "; ".join(protection.reasons))
    if protection.size_mult != 1.0:
        size_mult *= protection.size_mult
        add(9, "protections", "ADJUST", "; ".join(protection.reasons))
    else:
        add(9, "protections", "PASS", "no active protection")

    # --- gate 10: sanity --------------------------------------------------------------
    if not market.in_market_list:
        return reject(10, "sanity", f"{plan.symbol} is not in the exchange market list")
    for position in portfolio.positions:
        if position.symbol.upper() == plan.symbol.upper():
            return reject(10, "sanity", f"already {position.side} on {plan.symbol}")
    if plan.entry_kind == "market" and not plan.urgent:
        return reject(10, "sanity", "market entry without `urgent`")
    if plan.entry_kind == "market" and spread_pct > URGENT_MAX_SPREAD_PCT:
        return reject(
            10,
            "sanity",
            f"market entry with spread {spread_pct:.3f}% > {URGENT_MAX_SPREAD_PCT}%",
        )
    add(10, "sanity", "PASS", "symbol, side and entry style are consistent")

    # --- gate 11: kill-switch conflict ------------------------------------------------
    # The plan's own invalidation rules are normalised here (G-4 / F-013). They were journalled
    # raw and never parsed, so `[{"kind": "vibes"}]` was approved: a plan whose invalidation
    # cannot be evaluated has no invalidation, and an unparseable rule rejects the plan.
    try:
        result.rules = [parse_rule(raw) for raw in plan.invalidation_rules]
    except ValueError as exc:
        return reject(11, "kill-switch", f"invalidation rule refused: {exc}")
    if conflicts_with_plan(plan.side, portfolio.active_kill_switches):
        details = "; ".join(t.detail for t in portfolio.active_kill_switches)
        return reject(11, "kill-switch", f"plan fights an active kill-switch: {details}")
    # Portfolio-level switches (§9, review G-3). `peak_equity_30d` and `margin_ratio` reached the
    # gate and were never read: a 15 % drawdown closes everything and HALTs, a thin margin ratio
    # means reduce - and a new entry is the opposite of either.
    kill = portfolio_kill(
        portfolio.equity,
        portfolio.peak_equity_30d,
        portfolio.margin_ratio,
        has_open_positions=(
            bool(portfolio.positions)
            if portfolio.venue_positions is None
            else portfolio.venue_positions > 0
        ),
    )
    if kill.halt or kill.reduce or kill.block_entries:
        result.halt = result.halt or kill.halt
        return reject(11, "kill-switch", "; ".join(kill.reasons) + " — no new entries")
    add(11, "kill-switch", "PASS", "no conflicting kill-switch")

    # --- gate 14 (before sizing: it caps leverage) -------------------------------------
    liq_buffer = LIQ_BUFFER_MULTIPLE
    steps_used = steps
    if portfolio.downside_miss_rate is not None and not (
        0.0 <= portfolio.downside_miss_rate <= 1.0
    ):
        # NaN / inf / out of range: `>= limit` is False for NaN, which silently skipped the cap.
        return reject(
            14, "downside", f"downside_miss_rate {portfolio.downside_miss_rate!r} is not in [0, 1]"
        )
    if (
        portfolio.downside_miss_rate is not None
        and portfolio.downside_miss_rate >= DOWNSIDE_MISS_LIMIT
    ):
        result.leverage_cap = DOWNSIDE_LEVERAGE_CAP
        liq_buffer = DOWNSIDE_LIQ_BUFFER
        steps_used = tuple(
            StepPolicy(
                s.step,
                s.risk_pct,
                min(s.max_lev, DOWNSIDE_LEVERAGE_CAP),
                s.min_lev,
                s.max_notional_frac,
            )
            for s in steps
        )
        add(
            14,
            "downside-miss",
            "ADJUST",
            f"{portfolio.downside_miss_rate:.0%} of recent losers slipped past 1.3R — "
            f"leverage ≤{DOWNSIDE_LEVERAGE_CAP}, liquidation buffer {DOWNSIDE_LIQ_BUFFER}×",
        )

    # --- gates 4 and 5: size and leverage ---------------------------------------------
    def size_with(mult: float) -> SizingResult:
        return size_position(
            SizingInput(
                equity_usd=portfolio.equity,
                entry=entry,
                stop=plan.stop,
                side=plan.side,
                mark=mark,
                atr_pct=atr_pct,
                regime_mult=market.regime.risk_mult_for(plan.symbol, plan.side),
                protection_mult=mult,
                agent_mult=plan.size_factor,
                spec=market.spec,
                steps=steps_used,
                liq_buffer_multiple=liq_buffer,
            )
        )

    sized = size_with(size_mult)
    if not sized.ok:
        gate = 12 if sized.reason == "INSUFFICIENT_BALANCE" else 4
        return reject(gate, "size" if gate == 4 else "balance", sized.reason)
    result.sized = sized
    result.size_mult = size_mult
    add(
        4,
        "size",
        "PASS",
        f"${sized.notional_usd:,.2f} notional, step {sized.step}, risk ${sized.risk_usd:,.2f}",
    )
    add(
        5,
        "leverage",
        "PASS",
        f"{sized.leverage}x, liquidation {sized.liq_dist_pct:.1%} "
        f"vs stop {sized.stop_dist_pct:.1%}",
    )

    # --- gate 6: portfolio heat -------------------------------------------------------
    # The profile's cap (review G-5): aggressive holds at most two while equity < $500. The demo
    # exploration profile replaces it with its own flat cap.
    cap = max_positions if limits.max_positions is None else limits.max_positions
    if len(portfolio.positions) >= cap:
        return reject(6, "heat", f"{len(portfolio.positions)} open positions (max {cap})")
    if not (math.isfinite(portfolio.equity) and portfolio.equity > 0):
        return reject(6, "heat", f"equity {portfolio.equity} is not positive - heat unmeasurable")
    open_risk = sum(p.risk_usd for p in portfolio.positions) + sized.risk_usd
    open_risk_pct = open_risk / portfolio.equity * 100
    if open_risk_pct > limits.max_open_risk_pct:
        return reject(
            6, "heat", f"open risk {open_risk_pct:.2f}% > {limits.max_open_risk_pct}% of equity"
        )
    gross = sum(p.notional_usd for p in portfolio.positions) + sized.notional_usd
    gross_pct = gross / portfolio.equity * 100
    if gross_pct > MAX_GROSS_EXPOSURE_PCT:
        return reject(6, "heat", f"gross exposure {gross_pct:.0f}% > {MAX_GROSS_EXPOSURE_PCT:.0f}%")
    if beta > HIGH_BETA:
        same_side = [
            p
            for p in portfolio.positions
            if p.side == plan.side and (p.beta_btc is None or p.beta_btc > HIGH_BETA)
        ]
        if len(same_side) >= MAX_SAME_SIDE_HIGH_BETA:
            return reject(
                6,
                "heat",
                f"{len(same_side)} high-beta {plan.side}s already open "
                f"(max {MAX_SAME_SIDE_HIGH_BETA})",
            )
    add(6, "heat", "PASS", f"open risk {open_risk_pct:.2f}%, gross {gross_pct:.0f}%")

    # --- gate 7: funding --------------------------------------------------------------
    tp1_profit = _tp1_profit(plan, sized.notional_usd, entry)
    costs = expected_costs(
        sized.notional_usd,
        market.fees,
        ENTRY_IS_MAKER,
        funding_8h,
        plan.time_stop_hours,
        plan.side,
        interval,
    )
    if tp1_profit <= 0 or costs["funding"] > MAX_FUNDING_SHARE_OF_TP1 * tp1_profit:
        return reject(
            7,
            "funding",
            f"funding ${costs['funding']:.2f} over {plan.time_stop_hours}h > "
            f"{MAX_FUNDING_SHARE_OF_TP1:.0%} of ${tp1_profit:.2f} TP1 profit",
        )
    add(7, "funding", "PASS", f"funding ${costs['funding']:.2f} vs ${tp1_profit:.2f} TP1 profit")

    # --- gate 12: balance and minimum size --------------------------------------------
    if sized.margin_usd > portfolio.free_margin * FREE_MARGIN_BUFFER:
        return reject(
            12,
            "balance",
            f"INSUFFICIENT_BALANCE: needs ${sized.margin_usd:,.2f} margin, "
            f"${portfolio.free_margin * FREE_MARGIN_BUFFER:,.2f} usable",
        )
    add(
        12,
        "balance",
        "PASS",
        f"margin ${sized.margin_usd:,.2f} of ${portfolio.free_margin:,.2f} free",
    )

    # --- gate 13: fee viability -------------------------------------------------------
    viable, detail = fee_viable(
        sized.notional_usd,
        tp1_profit,
        market.fees,
        ENTRY_IS_MAKER,
        funding_8h,
        plan.time_stop_hours,
        plan.side,
        interval_hours=interval,
    )
    if not viable:
        return reject(13, "fees", detail)
    add(13, "fees", "PASS", detail)

    # --- cumulative adjustment --------------------------------------------------------
    # Compare the *multiplier*, not the rounded notional: lot-size rounding is not an
    # adjustment, and comparing notionals rejected a legitimately halved trade at 48%.
    if size_mult < MIN_SIZE_FRACTION_OF_IMPLIED:
        return reject(
            4,
            "size",
            f"adjustments shrank the trade to {size_mult:.0%} of its implied size — below "
            f"{MIN_SIZE_FRACTION_OF_IMPLIED:.0%}, so this is a different trade than the one scored",
        )

    result.status = "ADJUSTED" if any(r.verdict == "ADJUST" for r in result.reasons) else "APPROVED"
    result.reasons.sort(key=lambda r: r.gate)
    return result
