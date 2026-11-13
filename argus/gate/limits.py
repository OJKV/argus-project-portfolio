"""Decision limits per venue — the one place that says how strict a run is (DECISION_LOGIC §11).

Two profiles, and the venue alone picks between them:

* `LIVE_CONSERVATIVE` — the numbers the gate has always enforced. The module constants in
  `rules.py` and `protections.py` are bound to these fields, so their names and values do not move.
* `DEMO_EXPLORATION` — owner's grilling decisions of 2026-10-26: on the demo account only, let
the AI
  open as many trades as the fixed safety rails allow, so demo evidence can set future values.

The venue comes from `Settings.venue` (`OKX_DEMO`), never from anything an agent writes
(non-negotiable #3). Anything other than exactly ``"demo"`` gets the live profile, so an unknown
venue fails safe.

What a profile cannot touch (Q7): the exchange-side stop, stops-only-tighten, the liquidation buffer
and leverage cap, the 15 % kill switch, the 3 % daily / 6 % weekly halts, the event blackout, the
stop ATR band, per-trade risk sizing and the time-stop maximum. None of those is a field here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecisionLimits:
    name: str
    # gate 1
    min_score: int
    min_score_counter_regime: int
    # gate 4 (R:R) — also rendered into the Trader prompts
    min_rr_tp1: float
    min_rr_blended: float
    # gate 9 caps; `max_entries_per_run` is also the Trader batch's plan cap
    max_entries_per_run: int
    max_entries_per_day: int
    consecutive_loss_pause: int
    # gate 6. None = the risk profile decides (conservative 4, aggressive 2 under $500).
    max_positions: int | None
    max_open_risk_pct: float
    # gate 8: is CHOP limit-only? (EVENT_RISK always is.) The CHOP size multiplier is not here.
    chop_limit_only: bool
    # the adversarial chain (runs/pipeline.py)
    redteam_drop_severity: int  # severity at or above this drops the plan; 3..below halves it
    consensus_outage_proceeds: bool  # every model failed: proceed (True) or drop (False)
    riskdebate_decline_factor: float  # what "decline" multiplies the size by
    # the second opinion declined (one-sided): 0.0 = veto (live); 0.5 = halve the size (demo)
    consensus_one_sided_factor: float = 0.0
    # red team at or above `redteam_drop_severity`: 0.0 = drop (live); 0.25 = quarter size (demo)
    redteam_top_severity_factor: float = 0.0
    # targets too close to entry: False = the gate rejects (live); True = moved out to the floor
    repair_targets_to_min_rr: bool = False
    # stop wider than the gate 3 ATR band: False = the gate rejects (live); True = moved in to the
    # band edge when that is still beyond the 20-bar swing (demo, owner 2026-10-29). Never widens.
    repair_stops_to_atr_band: bool = False
    # the Trader prompts' closing stance: False = "no plans is a good outcome" (live text);
    # True = return the best candidates and express doubt in the scores (demo, 2026-10-26)
    explore: bool = False


LIVE_CONSERVATIVE = DecisionLimits(
    name="live_conservative",
    min_score=4,
    min_score_counter_regime=6,
    min_rr_tp1=1.5,
    min_rr_blended=2.0,
    max_entries_per_run=2,
    max_entries_per_day=3,
    consecutive_loss_pause=3,
    max_positions=None,
    max_open_risk_pct=5.0,
    chop_limit_only=True,
    redteam_drop_severity=4,
    consensus_outage_proceeds=False,
    riskdebate_decline_factor=0.0,
    explore=False,
)

DEMO_EXPLORATION = DecisionLimits(
    name="demo_exploration",
    min_score=2,
    min_score_counter_regime=4,
    min_rr_tp1=0.5,  # owner 2026-10-26 (was 1.0 in the grilling round)
    min_rr_blended=0.8,  # owner 2026-10-26 (was 1.3)
    max_entries_per_run=3,
    max_entries_per_day=8,
    consecutive_loss_pause=5,
    max_positions=6,
    max_open_risk_pct=8.0,
    chop_limit_only=False,
    redteam_drop_severity=5,
    consensus_outage_proceeds=True,
    riskdebate_decline_factor=0.5,
    explore=True,
    consensus_one_sided_factor=0.5,  # owner 2026-10-26
    repair_targets_to_min_rr=True,  # owner 2026-10-26
    redteam_top_severity_factor=0.25,  # owner 2026-10-27
    repair_stops_to_atr_band=True,  # owner 2026-10-29
)

# THE REVERT SWITCH. Set to LIVE_CONSERVATIVE to put the demo account back on the live numbers;
# nothing else needs to change (the extra demo-only manager slots then simply run conservatively).
DEMO_PROFILE: DecisionLimits = DEMO_EXPLORATION


def limits_for(venue: str) -> DecisionLimits:
    """The profile for a venue. Only the exact string "demo" relaxes anything."""
    return DEMO_PROFILE if venue == "demo" else LIVE_CONSERVATIVE
