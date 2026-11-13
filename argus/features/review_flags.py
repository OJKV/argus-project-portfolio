"""Deterministic facts about an open position, computed before the Reviewer sees it.

Measured on the golden set: the Reviewer acts on explicit flags reliably and infers poorly from
raw numbers in the dossier. It followed `tp1_hit` and `time_stop_breached` every time, and missed
funding at the 98th percentile and a thesis visibly weakening — both present, both as bare
figures.

So the division of labour is: **code computes what is checkable, the model judges what is not.**
Every flag here is a fact with one right answer. Nothing here is an opinion.
"""

from __future__ import annotations

from typing import Any

FUNDING_HOSTILE_PCTILE = 90.0
# A stop more than halfway to liquidation is too close. Same number the watchdog clamps
# emergency stops to (LIQUIDATION_CLAMP), so the two components agree about what is safe.
LIQUIDATION_PROXIMITY = 0.5
WEAK_VOLUME = 0.6
WEAK_OI_PCT = -12.0


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _target_price(target: Any) -> float | None:
    """`{"price": p, ...}` (the TradePlan shape) or `[p, close_pct]` (what the manager journals).

    Only the dict was read, so the first review of a journalled plan would have raised here.
    """
    if isinstance(target, dict):
        return _float(target.get("price"))
    if isinstance(target, list | tuple) and target:
        return _float(target[0])
    return None


def _close(timeframe: str, technical: dict[str, Any]) -> float | None:
    """The last CLOSED bar's close on the rule's timeframe; candle frames drop the forming bar."""
    stack = technical.get(f"ema_{timeframe.lower()}")
    close = _float(stack.get("close")) if isinstance(stack, dict) else None
    if close is None and timeframe.lower() == "4h":
        close = _float(technical.get("price"))
    return close


def _invalidation(
    plan: dict[str, Any], derivatives: dict[str, Any], technical: dict[str, Any]
) -> tuple[bool | None, list[str]]:
    """Whether a rule the plan wrote has fired, and the rules code could not check.

    `parse_rule` / `evaluate_plan_rules` had no caller (review L-3), so the plan's own invalidation
    was left for the model to find in a JSON blob. A rule that cannot be parsed, or whose input is
    missing, is reported as unchecked — never as "not hit", which a model would trust (F-013).
    So the answer has three values: True if a checked rule fired, False only when every rule was
    checked, and None ("unknown") when none fired but some could not be checked.
    """
    from argus.gate.killswitch import KillContext, evaluate_plan_rules, parse_rule

    hit = False
    unchecked: list[str] = []
    funding = _float(derivatives.get("funding_8h"))
    for raw in plan.get("invalidation_rules") or []:
        try:
            rule = parse_rule(raw if isinstance(raw, dict) else {"kind": raw})
        except (TypeError, ValueError):
            unchecked.append(str(raw)[:120])
            continue
        close = _close(rule.timeframe, technical)
        if (rule.kind.startswith("price_close") and close is None) or (
            rule.kind == "funding_flip" and funding is None
        ):
            unchecked.append(f"{rule.kind} {rule.level or rule.sign or ''}: input missing".strip())
            continue
        if rule.kind in ("event", "real_yield_above"):
            # No event outcomes or real-yield series reach a review; the model must judge these.
            unchecked.append(f"{rule.kind} {rule.event or rule.level or ''}".strip())
            continue
        ctx = KillContext(last_closed_price=close or 0.0, funding_8h=funding)
        hit = hit or bool(evaluate_plan_rules([rule], ctx))
    return (True if hit else None if unchecked else False), unchecked


def compute_flags(
    position: dict[str, Any],
    plan: dict[str, Any],
    derivatives: dict[str, Any],
    technical: dict[str, Any],
) -> dict[str, Any]:
    """Facts only. A missing input yields False rather than an exception: a review still has to
    happen when a data family is absent. The exception is `invalidation_hit`, which is None when
    a plan rule could not be checked: there "not hit" would be a claim, not a default."""
    side = str(position.get("side", "long"))
    long = side == "long"
    mark = _float(position.get("mark_price"))
    stop = _float(position.get("current_stop"))
    liq = _float(position.get("liq_price"))

    hours = _float(position.get("hours_in_trade")) or 0.0
    horizon = _float(plan.get("time_stop_hours"))

    targets = plan.get("targets") or []
    tp1 = _target_price(targets[0]) if targets else None
    tp1_hit = bool(tp1 is not None and mark is not None and (mark >= tp1 if long else mark <= tp1))

    pctile = _float(derivatives.get("funding_pctile_30d"))
    funding = _float(derivatives.get("funding_8h"))
    # Positive funding costs a long and pays a short: the same percentile is hostile to one side
    # and a subsidy to the other.
    costs_us = funding is not None and ((funding > 0) if long else (funding < 0))
    funding_hostile = bool(pctile is not None and pctile >= FUNDING_HOSTILE_PCTILE and costs_us)

    near_liq = False
    if None not in (mark, stop, liq) and mark != liq:
        # How far the stop sits along the road from mark to liquidation.
        room = abs(mark - liq)  # type: ignore[operator]
        gap = abs(mark - stop)  # type: ignore[operator]
        near_liq = room > 0 and gap / room >= LIQUIDATION_PROXIMITY

    rel_vol = _float(technical.get("rel_vol_4h"))
    oi = _float(derivatives.get("oi_24h_pct"))
    weakening = bool(
        rel_vol is not None and oi is not None and rel_vol < WEAK_VOLUME and oi <= WEAK_OI_PCT
    )

    invalidation_hit, unchecked = _invalidation(plan, derivatives, technical)

    flags: dict[str, Any] = {
        "tp1_hit": tp1_hit,
        "time_stop_breached": bool(horizon is not None and hours > horizon),
        "funding_hostile": funding_hostile,
        "stop_near_liquidation": near_liq,
        "thesis_weakening": weakening,
        "data_stale": bool(technical.get("stale")),
    }
    # Only for a plan with typed rules. Nothing to evaluate is not "not hit", and the Reviewer's
    # golden score (18/20) was measured on prompts without this key; emitting it everywhere
    # changed 18 of the 20 with no new measurement behind them.
    if plan.get("invalidation_rules"):
        flags["invalidation_hit"] = invalidation_hit
    if unchecked:
        flags["invalidation_unchecked"] = unchecked
    return flags
