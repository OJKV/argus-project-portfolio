"""The Analyst: what is true about this asset, before anyone decides what to do about it.

Kept separate from the Trader on purpose. A reading and a decision fail differently — the reading
can be wrong about the world, the decision can be wrong about the reading — and separating them
lets the Red Team attack one without the other. It also means a weak reading produces a weak
plan rather than a confident plan built on nothing.
"""

from __future__ import annotations

from argus.features.dossier import Dossier
from argus.features.dossier import render as render_dossier
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import AnalystView

AGENT = "analyst"
PROMPT = "analyst"


def build_prompt(dossier: Dossier) -> str:
    return render(
        PROMPT,
        dossier=render_dossier(dossier),
        regime=f"{dossier.regime.label} (risk x{dossier.regime.risk_mult})",
    )


def analyse(
    router: Router,
    run_id: str,
    dossier: Dossier,
    used_families: dict[str, str] | None = None,
) -> CallResult[AnalystView]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(dossier),
        output_type=AnalystView,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )
