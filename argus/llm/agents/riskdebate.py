"""Risk Debate: is taking this risk prudent now, alongside what is already open?

It sees what no single-trade check can: the book. The gate sizes one plan in isolation; this
agent notices that a "new" long is the third correlated one this week. It cannot increase size —
`RiskVerdict` has no value that means "more" — and `apply_verdict` clamps regardless.
"""

from __future__ import annotations

import json
from typing import Any

from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import RiskVerdict

AGENT = "riskdebate"
PROMPT = "riskdebate"

# What each recommendation does to the gate's size. Nothing above 1.0.
SIZE_FACTOR: dict[str, float] = {"proceed": 1.0, "reduce": 0.5, "decline": 0.0}


def _dump(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def build_prompt(
    plan: dict[str, Any],
    portfolio: dict[str, Any],
    regime: str,
    calendar: list[dict[str, Any]] | None = None,
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> str:
    from argus.features.risk_flags import compute_risk_flags

    return render(
        PROMPT,
        plan=_dump(plan),
        portfolio=_dump(portfolio),
        regime=regime,
        calendar=_dump(calendar or []),
        flags=_dump(
            compute_risk_flags(plan, portfolio, calendar, budget_pct=limits.max_open_risk_pct)
        ),
    )


def debate(
    router: Router,
    run_id: str,
    plan: dict[str, Any],
    portfolio: dict[str, Any],
    regime: str,
    calendar: list[dict[str, Any]] | None = None,
    used_families: dict[str, str] | None = None,
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> CallResult[RiskVerdict]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(plan, portfolio, regime, calendar, limits),
        output_type=RiskVerdict,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )


def apply_verdict(verdict: RiskVerdict, limits: DecisionLimits = LIVE_CONSERVATIVE) -> float:
    """The factor to apply to the gate's size. Clamped to 1.0 whatever the table says.

    "decline" is the profile's factor: 0.0 (drop) on live, 0.5 (as "reduce") on demo.
    """
    if verdict.recommendation == "decline":
        return min(1.0, max(0.0, limits.riskdebate_decline_factor))
    return min(1.0, SIZE_FACTOR.get(verdict.recommendation, 1.0))
