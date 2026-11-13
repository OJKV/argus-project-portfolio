"""Demo-only plan repairs: targets (owner, 2026-10-26) and stops (owner, 2026-10-29).

Free models keep placing the first target at the nearest level even when it is far inside the
R:R floor (0.15R against 0.5R on 2026-10-26). On a profile with `repair_targets_to_min_rr`, the
gate's own floor is applied to the plan instead of rejecting it: targets that sit too close move
out to exactly the floor (plus a hair, so rounding cannot drop them back under). The stop, the
entry, the side and the close percentages never change, so the risk of the trade is unchanged —
only the reward it waits for. Live (`LIVE_CONSERVATIVE`) never repairs.

Stops: free models put the stop at a distant level (4.9-6.6% on 2026-10-28) beyond gate 3's
3.5x ATR ceiling. With `repair_stops_to_atr_band` the stop moves *in* to just inside that ceiling,
but only if it is then still beyond the 20-bar swing; otherwise the plan is left for the gate to
reject. A stop is never widened. Sizing then works from the tighter stop, so the dollar risk is the
same budget as any other trade, on a stop that is hit more easily.
"""

from __future__ import annotations

import dataclasses
import math

from argus.gate.limits import DecisionLimits
from argus.gate.rules import STOP_ATR_BAND, MarketContext, PlanInput, Target

_HAIR = 0.01  # R added above the floor so tick rounding at placement cannot undo the repair
_STOP_HAIR_ATR = 0.05  # ATR kept inside the band edge, for the same reason


def repair_stop(
    plan: PlanInput, limits: DecisionLimits, market: MarketContext
) -> tuple[PlanInput, str | None]:
    """The plan with a too-wide stop moved in to the ATR band edge, and a note; or untouched."""
    if not limits.repair_stops_to_atr_band:
        return plan, None
    atr_pct = market.atr_pct
    swing = market.swing_low_20 if plan.side == "long" else market.swing_high_20
    mark = market.mark
    entry = mark if plan.entry_kind == "market" and mark else plan.entry
    if not (atr_pct and swing and math.isfinite(atr_pct + swing + entry)) or atr_pct <= 0:
        return plan, None
    sign = 1.0 if plan.side == "long" else -1.0  # stop sits at entry - sign * distance
    dist_pct = sign * (entry - plan.stop) / entry * 100
    max_pct = (STOP_ATR_BAND[1] - _STOP_HAIR_ATR) * atr_pct
    if dist_pct <= STOP_ATR_BAND[1] * atr_pct:
        return plan, None  # inside the band (or on the wrong side): nothing to tighten
    stop = entry * (1 - sign * max_pct / 100)
    # Beyond the swing by the same hair: tick rounding at placement pulls a stop toward entry.
    if sign * (swing - stop) <= _STOP_HAIR_ATR * atr_pct / 100 * entry:
        return plan, None  # the band edge is inside structure: the gate rejects as before
    note = (
        f"stop repaired to the ATR band: {plan.stop:g} ({dist_pct:.2f}%) -> "
        f"{stop:g} ({max_pct:.2f}%), swing {swing:g}"
    )
    return dataclasses.replace(plan, stop=stop), note


def repair_targets(
    plan: PlanInput, limits: DecisionLimits, *, mark: float | None
) -> tuple[PlanInput, str | None]:
    """The plan with targets moved out to the profile's floor, and a note; or the plan untouched."""
    if not limits.repair_targets_to_min_rr or not plan.targets:
        return plan, None
    entry = mark if plan.entry_kind == "market" and mark is not None else plan.entry
    risk = abs(entry - plan.stop)
    if risk <= 0:
        return plan, None
    sign = 1.0 if plan.side == "long" else -1.0
    rs = [sign * (t.price - entry) / risk for t in plan.targets]
    weights = [t.close_pct / 100 for t in plan.targets]

    floors = [limits.min_rr_tp1 + _HAIR] + [0.0] * (len(rs) - 1)
    if len(rs) == 1:
        floors[0] = max(limits.min_rr_tp1, limits.min_rr_blended) + _HAIR
    new = [max(r, f) for r, f in zip(rs, floors, strict=True)]
    blended = sum(r * w for r, w in zip(new, weights, strict=True))
    if blended < limits.min_rr_blended and len(new) > 1:
        rest_weight = sum(weights[1:])
        need = (limits.min_rr_blended + _HAIR - new[0] * weights[0]) / rest_weight
        new = [new[0]] + [max(r, need) for r in new[1:]]
    for i in range(1, len(new)):  # keep targets ordered away from entry
        new[i] = max(new[i], new[i - 1])

    if all(abs(a - b) < 1e-12 for a, b in zip(new, rs, strict=True)):
        return plan, None
    targets = [
        Target(price=entry + sign * r * risk, close_pct=t.close_pct)
        for r, t in zip(new, plan.targets, strict=True)
    ]
    note = f"targets repaired to the {limits.name} floor: " + ", ".join(
        f"{a:.2f}R→{b:.2f}R" for a, b in zip(rs, new, strict=True)
    )
    return dataclasses.replace(plan, targets=targets), note
