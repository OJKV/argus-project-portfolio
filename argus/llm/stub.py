"""Deterministic stand-in for the Analyst → Red Team → Trader chain (Phase 3 scaffolding).

It exists so the pipeline can be exercised end to end before the LLM keys arrive: it turns
indicator values into a plan-shaped proposal so the Risk Gate has something real to judge.

**It is not a strategy.** Scores are mechanical restatements of the dossier, not evidence-cited
judgements, so `propose()` refuses to run outside a shadow run. `argus/llm/agents/` replaces it
in Prompt 7.
"""

from __future__ import annotations

from typing import Any

from argus.features.dossier import Dossier
from argus.gate.rules import FactorScores, MarketContext, PlanInput, Target

STOP_ATR_MULT = 2.0
TP1_R, TP2_R = 1.6, 3.0


def _trend_score(dossier: Dossier) -> int:
    """+2 aligned stacks, +1 one aligned, 0 mixed, mirrored for down-stacks."""
    orders = [(dossier.technical.get(f"ema_{tf}") or {}).get("order") for tf in ("1h", "4h", "1d")]
    bulls, bears = orders.count("bull"), orders.count("bear")
    if bulls >= 2:
        return 2 if bulls == 3 else 1
    if bears >= 2:
        return -2 if bears == 3 else -1
    return 0


def propose(dossier: Dossier, market: MarketContext, shadow: bool = True) -> PlanInput | None:
    """A mechanical long/short template, or None when nothing lines up (NO_TRADE is normal)."""
    if not shadow:
        raise NotImplementedError("the stub proposer may only run in a shadow run")
    if not dossier.trade_critical_present:
        return None
    price = dossier.technical.get("price")
    atr_pct = dossier.technical.get("atr_4h_pct")
    if not price or not atr_pct:
        return None

    trend = _trend_score(dossier)
    if trend == 0:
        return None
    side = "long" if trend > 0 else "short"
    stop_distance = price * (atr_pct / 100) * STOP_ATR_MULT
    stop = price - stop_distance if side == "long" else price + stop_distance
    direction = 1 if side == "long" else -1
    targets = [
        Target(round(price + direction * stop_distance * TP1_R, 6), 50),
        Target(round(price + direction * stop_distance * TP2_R, 6), 50),
    ]
    rsi = dossier.technical.get("rsi_4h") or 50
    funding = dossier.derivatives.get("funding_8h") or 0.0
    scores = FactorScores(
        fundamental=0,
        onchain=0,
        macro=0,
        catalyst=1 if dossier.catalysts else 0,
        technical=max(-2, min(2, trend + (1 if 40 <= rsi <= 70 else 0))),
    )
    # Crowded funding on our own side is the one thing the stub will argue itself out of.
    if (side == "long" and funding > 0.0005) or (side == "short" and funding < -0.0005):
        return None
    return PlanInput(
        symbol=dossier.symbol,
        side=side,  # type: ignore[arg-type]
        entry_kind="limit",
        entry_low=round(price * 0.999, 6),
        entry_high=round(price * 1.001, 6),
        stop=round(stop, 6),
        targets=targets,
        time_stop_hours=120,
        scores=scores,
        invalidation_rules=[
            {
                "kind": "price_close_below" if side == "long" else "price_close_above",
                "level": round(stop, 6),
                "timeframe": "1d",
            }
        ],
    )


def describe() -> dict[str, Any]:
    return {"proposer": "stub", "note": "mechanical template — replaced by the LLM chain in P7"}
