"""Arithmetic about a closed trade, computed once so the post-mortem never has to derive it.

The same lesson as `review_flags.py` and `risk_flags.py`, learned three times now (F-102, F-110,
F-111): models judge well and calculate badly, and every fact that can be computed from the plan
and the fills should arrive already computed. The specific failure here was a trade filled 4.3%
above its planned trigger — a number sitting in two fields the model never subtracted — being
graded `entry_timing: good`.

None of these flags carry a verdict. `stop_distance_atr = 0.4` is a fact; whether 0.4 was too
tight for this setup is the judgement, and that stays with the model.
"""

from __future__ import annotations

from typing import Any


def _f(source: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = source.get(name)
        if isinstance(value, int | float):
            return float(value)
    return None


def compute_trade_flags(plan: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    entry = _f(plan, "entry")
    stop = _f(plan, "stop")
    atr = _f(plan, "atr_14", "atr")
    fill = _f(outcome, "fill_price") or entry
    side = str(plan.get("side", "long"))

    if entry and fill:
        slip = (fill - entry) / entry * 100.0
        # Signed against the trader: positive means the fill was worse than the plan, whichever
        # way the trade was facing.
        flags["entry_slippage_pct"] = round(slip if side == "long" else -slip, 2)
    if entry and stop:
        risk = abs(entry - stop)
        flags["planned_risk_pct"] = round(risk / entry * 100.0, 2)
        if atr:
            flags["stop_distance_atr"] = round(risk / atr, 2)
    if (held := _f(outcome, "hours_held")) is not None:
        flags["hours_held"] = held
        if limit := _f(plan, "time_stop_hours"):
            flags["hit_time_stop"] = held >= limit
    if (mfe := _f(outcome, "max_favourable_r")) is not None:
        flags["max_favourable_r"] = mfe
    thesis = str(outcome.get("thesis_at_exit", ""))
    if thesis:
        flags["thesis_broken_at_exit"] = thesis.lower().startswith("broken")
    if (r := _f(outcome, "outcome_r")) is not None and (
        btc := _f(outcome, "btc_return_same_window_pct")
    ) is not None:
        # Did the trade lose while its market rose? A loss into a rising tape is a different
        # question from a loss into a falling one, and the answer is a subtraction.
        flags["fought_the_tape"] = r < 0 and btc > 1.0
    return flags
