"""The Trader seeing every candidate at once — one call per run, as the design always said.

The per-asset version (still in `trader.py`, still used by the golden suite) inherited its shape
from the deterministic stub it replaced: one dossier in, one answer out, fifteen times. That is
not only fifteen times the cost. Fifteen independent calls cannot rank anything — each answers
"is this tradeable?" in isolation, so the gate approves the first plan that passes rather than the
best one available, and nothing ever notices that three candidates are the same bet (F-117).

What it still cannot do is decide how much. There is no size or leverage field here either; the
gate sizes, and this agent only says which trades are worth having.
"""

from __future__ import annotations

import json
from typing import Any

from argus.features.dossier import Dossier
from argus.features.dossier import render as render_dossier
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.llm.agents.trader import rr_values, stance
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import MAX_PLANS_PER_RUN, TradePlan, TraderBatch, TraderBatchOfThree

AGENT = "trader"
PROMPT = "trader_batch"


_WORDS = {1: "one", 2: "two", 3: "three", 4: "four"}


def batch_schema(limits: DecisionLimits) -> type[TraderBatch]:
    """The output schema for the profile's plan cap. Live keeps `TraderBatch` byte for byte."""
    if limits.max_entries_per_run == MAX_PLANS_PER_RUN:
        return TraderBatch
    if limits.max_entries_per_run == 3:
        return TraderBatchOfThree
    raise ValueError(f"no TraderBatch schema for {limits.max_entries_per_run} plans")


def build_prompt(
    dossiers: list[Dossier],
    portfolio: dict[str, Any],
    lessons: str = "-",
    traps: str = "-",
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> str:
    """Every candidate in one block, newest research first, with the book they would join."""
    max_plans = limits.max_entries_per_run
    blocks = [
        f'<candidate symbol="{d.symbol}">\n{render_dossier(d)}\n</candidate>' for d in dossiers
    ]
    first = dossiers[0] if dossiers else None
    return render(
        PROMPT,
        regime=(f"{first.regime.label} (risk x{first.regime.risk_mult})" if first else "unknown"),
        portfolio=json.dumps(portfolio, default=str, separators=(",", ":")),
        candidates="\n".join(blocks) or "(no candidates cleared research)",
        lessons=lessons,
        traps=traps,
        max_plans=str(max_plans),
        max_plans_word=_WORDS[max_plans],
        no_trade_stance=stance(limits, batch=True),
        **rr_values(limits),
    )


def drop_held(dossiers: list[Dossier], held: set[str]) -> tuple[list[Dossier], list[str]]:
    """The candidates the book does not already hold, and the symbols dropped.

    Gate 10 refuses any plan on a held symbol, so offering one only spends a Trader pick on a
    plan that cannot be placed (owner 2026-10-28: 4 of 5 plans were BTC while BTC was held).
    """
    held_lower = {h.lower() for h in held}
    kept = [d for d in dossiers if d.symbol.lower() not in held_lower]
    dropped = [d.symbol for d in dossiers if d.symbol.lower() in held_lower]
    return kept, dropped


def offered_symbols(dossiers: list[Dossier]) -> set[str]:
    return {d.symbol for d in dossiers}


def keep_offered(answer: TraderBatch, offered: set[str]) -> tuple[list[TradePlan], list[str]]:
    """Split the plans into (on an offered symbol, invented).

    The schema cannot check this — only the caller knows what was offered — and a plan on a symbol
    nobody researched has no dossier behind it, so nothing downstream could gate it honestly.
    """
    kept = [p for p in answer.plans if p.symbol in offered]
    return kept, [p.symbol for p in answer.plans if p.symbol not in offered]


def propose(
    router: Router,
    run_id: str,
    dossiers: list[Dossier],
    portfolio: dict[str, Any],
    lessons: str = "-",
    traps: str = "-",
    used_families: dict[str, str] | None = None,
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> CallResult[TraderBatch]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(dossiers, portfolio, lessons, traps, limits),
        output_type=batch_schema(limits),
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )
