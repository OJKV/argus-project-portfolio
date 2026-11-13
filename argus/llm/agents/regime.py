"""The Regime agent: how brave the rest of the run is allowed to be.

It runs once per cycle before any asset is examined, and it cannot increase risk. The
deterministic classifier in `features/regime.py` sets the label and the multipliers from price
structure and breadth; this agent reads the context around that arithmetic and may ask for
*less*. `RegimeCall` has no posture that means "more", which is the same protection as a
TradePlan having no size field.
"""

from __future__ import annotations

import json
from typing import Any

from argus.features.regime import RegimeView
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import RegimeCall

AGENT = "regime"
PROMPT = "regime"

# What the agent may ask the run to do, as a multiplier on the computed risk. Nothing above 1.0.
POSTURE_MULTIPLIER: dict[str, float] = {"normal": 1.0, "reduce": 0.5, "stand_aside": 0.0}


def build_prompt(
    computed: RegimeView,
    market: dict[str, Any],
    calendar: list[dict[str, Any]] | None = None,
) -> str:
    return render(
        PROMPT,
        market=json.dumps(market, default=str, separators=(",", ":")),
        calendar=json.dumps(calendar or [], default=str, separators=(",", ":")),
        computed_label=computed.label,
        computed_reasons="; ".join(computed.reasons) or "no reasons recorded",
    )


def read_regime(
    router: Router,
    run_id: str,
    computed: RegimeView,
    market: dict[str, Any],
    calendar: list[dict[str, Any]] | None = None,
    used_families: dict[str, str] | None = None,
) -> CallResult[RegimeCall]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(computed, market, calendar),
        output_type=RegimeCall,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )


def apply_posture(computed: RegimeView, call: RegimeCall) -> float:
    """The risk multiplier after the agent's posture. Never above the computed one.

    Clamped rather than trusted: if a future prompt or schema change ever admitted a posture
    meaning "more", this function would still refuse to return it.
    """
    factor = POSTURE_MULTIPLIER.get(call.posture, 1.0)
    return min(computed.risk_mult, computed.risk_mult * factor)
