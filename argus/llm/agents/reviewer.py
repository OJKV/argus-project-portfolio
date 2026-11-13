"""The Reviewer: hold, trim, close or tighten an open position.

Entering is one decision; reviewing is every decision after it. This agent runs against every
open position on every run, so it sees far more of the trading outcome than the Trader does —
which is why it sits on the reasoning tier rather than the cheapest one.

Its authority is deliberately narrow. It cannot size, it cannot open, and the one destructive
thing it could do — widen a stop — is refused by the schema rather than by the prompt.
"""

from __future__ import annotations

import json
import math
from typing import Any

from argus.features.dossier import Dossier
from argus.features.dossier import render as render_dossier
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import ReviewDecision

AGENT = "reviewer"
PROMPT = "reviewer"

# Account-level money. The account's exact size identifies it across providers that log; to two
# significant figures it is still the context the model needs (review L-12).
_ACCOUNT_MONEY = ("equity", "free", "balance", "available")


def _two_figures(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number == 0 or not math.isfinite(number):
        return number
    return float(f"{number:.2g}")


def _private(
    position: dict[str, Any], portfolio: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """What leaves the process: no raw venue row, no exact account size (review L-12)."""
    shown = {k: v for k, v in position.items() if k != "raw"}
    book = {
        k: _two_figures(v) if any(word in k.lower() for word in _ACCOUNT_MONEY) else v
        for k, v in portfolio.items()
    }
    return shown, book


def build_prompt(
    position: dict[str, Any],
    plan: dict[str, Any],
    dossier: Dossier,
    flags: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
) -> str:
    """Position state, the original plan, a fresh dossier, and the flags code already knows.

    The flags matter: `tp1_hit`, `time_stop_breached` and `invalidation_hit` are computed
    deterministically (`features/review_flags.py`), so asking a model to re-derive them is both
    wasteful and less reliable. `invalidation_hit` is present only when the plan has typed
    `invalidation_rules`, and is null when none fired but some could not be evaluated; those
    are listed under `invalidation_unchecked` for the model to judge. This docstring used to name
    `invalidation_hit` while nothing computed it (review L-3).
    """
    shown, book = _private(position, portfolio or {})
    return render(
        PROMPT,
        position=json.dumps(shown, default=str, separators=(",", ":")),
        plan=json.dumps(plan, default=str, separators=(",", ":")),
        dossier=render_dossier(dossier),
        flags=json.dumps(flags or {}, default=str),
        portfolio=json.dumps(book, default=str, separators=(",", ":")),
    )


def review(
    router: Router,
    run_id: str,
    position: dict[str, Any],
    plan: dict[str, Any],
    dossier: Dossier,
    flags: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    used_families: dict[str, str] | None = None,
) -> CallResult[ReviewDecision]:
    # `reviewer: trader` is an adversarial pair: a Reviewer from the lab that wrote the plan is an
    # echo (L-13). The journaled plan names that family; it is routing, not something to judge.
    plan = dict(plan)
    trader_family = plan.pop("trader_family", None)
    if used_families is None and trader_family:
        used_families = {"trader": str(trader_family)}
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(position, plan, dossier, flags, portfolio),
        output_type=ReviewDecision,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )
