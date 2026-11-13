"""Kill-switches — DECISION_LOGIC.md §9, and gate 11 (plan contradicts an active switch).

Plan-level rules come from the LLM's `invalidation_rules` and are **normalised into a closed set
of typed rules here** — an unparseable rule is rejected, never ignored, because a plan whose
invalidation cannot be evaluated has no invalidation (FAILURE_LOG F-013).

Everything is evaluated on closed candles: an intrabar wick is not a close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

PORTFOLIO_DD_LIMIT_PCT = 15.0
MARGIN_RATIO_FLOOR_PCT = 200.0

RuleKind = Literal[
    "price_close_above", "price_close_below", "funding_flip", "event", "real_yield_above"
]
NEEDS_LEVEL = {"price_close_above", "price_close_below", "real_yield_above"}


@dataclass(frozen=True)
class KillRule:
    kind: RuleKind
    level: float | None = None
    sign: str | None = None
    event: str | None = None
    outcome: str | None = None
    timeframe: str = "1d"


@dataclass(frozen=True)
class Triggered:
    rule: KillRule
    detail: str
    # The side of the open position whose invalidation rule fired (DECISION_LOGIC gate 11): new
    # entries on that side are blocked. None = unknown, which blocks both sides (fail closed).
    side: str | None = None


@dataclass(frozen=True)
class KillContext:
    last_closed_price: float
    funding_8h: float | None = None
    real_yield_10y: float | None = None
    event_outcomes: dict[str, str] = field(default_factory=dict)


@dataclass
class PortfolioKill:
    close_all: bool = False
    halt: bool = False
    reduce: bool = False
    # Refuse NEW entries only; never flatten or reduce on the strength of a missing reading.
    block_entries: bool = False
    reasons: list[str] = field(default_factory=list)


KINDS = frozenset(
    {"price_close_above", "price_close_below", "funding_flip", "event", "real_yield_above"}
)
SIGNS = frozenset({"positive", "negative"})
# The bars the system measures; a rule on any other timeframe could never be evaluated honestly.
TIMEFRAMES = frozenset({"4h", "1d"})


def _level(kind: str, value: Any) -> float:
    """A finite number; positive for a price. Bools are numbers in Python, not levels here."""
    if value is None:
        raise ValueError(f"{kind} needs a level")
    if isinstance(value, bool):
        raise ValueError(f"{kind} level must be a number, not {value!r}")
    try:
        level = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{kind} level {value!r} is not a number") from exc
    if not math.isfinite(level):
        raise ValueError(f"{kind} level {value!r} is not finite")
    if kind.startswith("price_") and level <= 0:
        raise ValueError(f"{kind} level {level} is not a price")
    return level


def _text(raw: dict[str, Any], key: str, kind: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{kind} rule needs a non-empty {key!r}")
    return value.strip()


def parse_rule(raw: dict[str, Any]) -> KillRule:
    """One LLM-written rule into the closed set, or ValueError.

    Review G-16: this accepted rules that could never fire (NaN/inf/negative levels, an `event`
    with nothing to match) and one that always fired (`funding_flip` with no sign compared
    "negative" against None and fired on any funding >= 0). Each is now refused with a reason.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"kill-switch rule must be a mapping, got {type(raw).__name__}")
    kind = str(raw.get("kind", "")).lower()
    if kind not in KINDS:
        raise ValueError(f"unknown kill-switch rule: {raw.get('kind')!r}")
    timeframe = str(raw.get("timeframe", "1d")).lower()
    if timeframe not in TIMEFRAMES:
        raise ValueError(
            f"{kind} timeframe {raw.get('timeframe')!r} is not one of {sorted(TIMEFRAMES)}"
        )
    level = _level(kind, raw.get("level")) if kind in NEEDS_LEVEL else None
    sign = None
    if kind == "funding_flip":
        sign = str(raw.get("sign", "")).lower()
        if sign not in SIGNS:
            raise ValueError(f"funding_flip sign {raw.get('sign')!r} is not one of {sorted(SIGNS)}")
    event = outcome = None
    if kind == "event":
        event, outcome = _text(raw, "event", kind), _text(raw, "outcome", kind)
    return KillRule(
        kind=kind,  # type: ignore[arg-type]
        level=level,
        sign=sign,
        event=event,
        outcome=outcome,
        timeframe=timeframe,
    )


def evaluate_plan_rules(
    rules: list[KillRule], ctx: KillContext, side: str | None = None
) -> list[Triggered]:
    """`side` = the side of the position the rules belong to; stamped on each Triggered."""
    fired: list[Triggered] = []
    for rule in rules:
        if rule.kind == "price_close_above" and rule.level is not None:
            if ctx.last_closed_price > rule.level:
                fired.append(
                    Triggered(
                        rule, f"{rule.timeframe} close {ctx.last_closed_price} > {rule.level}", side
                    )
                )
        elif rule.kind == "price_close_below" and rule.level is not None:
            if ctx.last_closed_price < rule.level:
                fired.append(
                    Triggered(
                        rule, f"{rule.timeframe} close {ctx.last_closed_price} < {rule.level}", side
                    )
                )
        elif rule.kind == "funding_flip" and ctx.funding_8h is not None:
            negative = ctx.funding_8h < 0
            wants_negative = rule.sign == "negative"
            if negative == wants_negative:
                fired.append(Triggered(rule, f"funding {ctx.funding_8h:+.6f} is {rule.sign}", side))
        elif rule.kind == "real_yield_above" and ctx.real_yield_10y is not None:
            if rule.level is not None and ctx.real_yield_10y > rule.level:
                fired.append(
                    Triggered(
                        rule, f"real yield {ctx.real_yield_10y:.2f}% > {rule.level:.2f}%", side
                    )
                )
        elif rule.kind == "event" and rule.event:
            outcome = ctx.event_outcomes.get(rule.event)
            if outcome is not None and outcome == rule.outcome:
                fired.append(Triggered(rule, f"{rule.event} resolved {outcome}", side))
    return fired


def conflicts_with_plan(side: str, fired: list[Triggered]) -> bool:
    """Gate 11: a fired invalidation rule of an open position blocks new entries on THAT
    position's side, for every rule kind (an open long's price_close_below -> no new longs).
    A trigger that does not know its side blocks every entry: unknown is not safe."""
    return any(t.side is None or t.side == side for t in fired)


def portfolio_kill(
    equity: float,
    peak_30d: float,
    margin_ratio: float | None,
    has_open_positions: bool = True,
) -> PortfolioKill:
    kill = PortfolioKill()
    if not (math.isfinite(equity) and math.isfinite(peak_30d)):
        kill.block_entries = True
        kill.reasons.append(f"equity {equity} / 30d high {peak_30d} is not a finite number")
    elif peak_30d <= 0:
        # No usable high to measure a drawdown against: say so, and keep new risk off.
        kill.block_entries = True
        kill.reasons.append(f"30d equity high {peak_30d} is not positive - drawdown unmeasurable")
    else:
        drawdown = (peak_30d - equity) / peak_30d * 100
        if drawdown >= PORTFOLIO_DD_LIMIT_PCT:
            kill.close_all = kill.halt = True
            kill.reasons.append(
                f"equity drawdown {drawdown:.1f}% >= {PORTFOLIO_DD_LIMIT_PCT:.0f}% of the 30d high"
            )
    if margin_ratio is None or not math.isfinite(margin_ratio):
        if has_open_positions:
            kill.block_entries = True
            kill.reasons.append("margin ratio unknown with open positions - no new entries")
    elif margin_ratio < MARGIN_RATIO_FLOOR_PCT:
        kill.reduce = True
        kill.reasons.append(
            f"margin ratio {margin_ratio:.0f}% < {MARGIN_RATIO_FLOOR_PCT:.0f}% — reduce"
        )
    return kill
