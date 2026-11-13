"""The Red Team: attack the reading, not the trade.

Its value is entirely in being wrong-footed rarely and right loudly. Two failure modes destroy
that: objecting to everything, which makes it noise that gets ignored precisely when it matters,
and agreeing with everything, which makes it decoration. Both are tested.

It runs on a different model family from the Analyst by construction (`adversarial` in
models.yaml) — a critique from the same family mostly reproduces the same blind spots in
different words.
"""

from __future__ import annotations

from typing import Any

from argus.features.dossier import Dossier
from argus.features.dossier import render as render_dossier
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import AnalystView, RedTeamVerdict

AGENT = "redteam"
PROMPT = "redteam"

# The severity at which an objection should change a decision. Measured 2026-09-25 on the golden
# suite: `agree` is near-constant False on sound readings (F-136), while severity separated sound
# from unsound 8 times in 9. One constant for the scorer and the live chain, so the line the role
# is graded on is the line it is used at.
ACTIONABLE_SEVERITY = 3


def build_prompt(dossier: Dossier, analysis: AnalystView | dict[str, Any]) -> str:
    payload = analysis if isinstance(analysis, dict) else analysis.model_dump()
    import json

    return render(
        PROMPT,
        dossier=render_dossier(dossier),
        analysis=json.dumps(payload, default=str, separators=(",", ":")),
    )


def challenge(
    router: Router,
    run_id: str,
    dossier: Dossier,
    analysis: AnalystView | dict[str, Any],
    used_families: dict[str, str] | None = None,
) -> CallResult[RedTeamVerdict]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(dossier, analysis),
        output_type=RedTeamVerdict,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )


def challenge_plan(
    router: Router,
    run_id: str,
    dossier: Dossier,
    plan: dict[str, Any],
    trader_family: str,
) -> CallResult[RedTeamVerdict]:
    """Attack the Trader's plan as the reading it is: cited factor scores, a thesis and levels.

    The Analyst this role was first built against never runs in the live path, and adding it would
    be a second model call per candidate to produce a reading the Trader already wrote. The prompt's
    five checks — uncited scores, ignored contradictions, opinion as fact, overconfidence, invented
    levels — apply to a plan unchanged. The Trader's family is excluded, not deprioritised.
    """
    return challenge(router, run_id, dossier, plan, used_families={"trader": trader_family})


def size_factor(verdict: RedTeamVerdict, limits: DecisionLimits = LIVE_CONSERVATIVE) -> float:
    """Severity, never `agree`: below the line nothing changes, from it the trade halves, and at the
    profile's drop severity (live 4, demo 5) the reading cannot be relied on and the trade is
    dropped. It can only ever shrink a trade."""
    if verdict.severity < ACTIONABLE_SEVERITY:
        return 1.0
    if verdict.severity < limits.redteam_drop_severity:
        return 0.5
    # Live drops (0.0). Demo shrinks to a quarter instead (owner 2026-10-27).
    return limits.redteam_top_severity_factor
