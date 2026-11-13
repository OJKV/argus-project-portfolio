"""The second opinion, and the code that compares it.

The Consensus Trader answers the *same* question as the Trader, from the same dossier, without
seeing the first answer. Showing it the first plan would anchor it, and an anchored second
opinion is a rubber stamp with extra latency.

The comparison itself is pure code on purpose. Asking a model "do these two agree?" reintroduces
the judgement the second opinion exists to check, and a persuasive rationale could paper over a
direction mismatch. Direction and risk are arithmetic; arithmetic belongs here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from argus.features.dossier import Dossier
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.llm.agents.trader import build_prompt
from argus.llm.prompts import load
from argus.llm.router import CallResult, Router
from argus.llm.schemas import TradePlan, TraderAnswer

AGENT = "consensus"
PROMPT = "trader"
# Two stops more than this far apart describe different trades even when the direction matches.
STOP_DIVERGENCE = 0.35


class Agreement(StrEnum):
    AGREE = "agree"
    CONFLICT = "conflict"  # opposite directions
    ONE_SIDED = "one_sided"  # one wants the trade, the other does not
    DIVERGENT = "divergent"  # same direction, incompatible risk


@dataclass(frozen=True)
class ConsensusResult:
    agreement: Agreement
    tradeable: bool
    detail: str
    plan: TradePlan | None = None


def second_opinion(
    router: Router,
    run_id: str,
    dossier: Dossier,
    trader_family: str,
    lessons: str = "-",
    traps: str = "-",
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> CallResult[TraderAnswer]:
    """Ask again on a different family. `trader_family` is excluded, not merely deprioritised.

    Same R:R bars as the first Trader: a second opinion held to a stricter bar would manufacture
    a ONE_SIDED veto out of a threshold, not a judgement."""
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(dossier, lessons, traps, limits),
        output_type=TraderAnswer,
        used_families={"trader": trader_family},
        prompt_version=load(PROMPT).label,
    )


def compare(primary: TraderAnswer, second: TraderAnswer) -> ConsensusResult:
    """Do the two answers describe the same trade? Direction first, then risk."""
    a, b = primary.plan, second.plan

    if a is None and b is None:
        return ConsensusResult(Agreement.AGREE, False, "both declined")
    if a is None or b is None:
        # A veto, not a tie. A trade needs both to want it.
        who = "the primary" if a is None else "the second opinion"
        return ConsensusResult(Agreement.ONE_SIDED, False, f"{who} declined the trade")
    if a.side != b.side:
        return ConsensusResult(
            Agreement.CONFLICT,
            False,
            f"opposite directions: {a.side} vs {b.side} — at least one misread the dossier",
        )

    entry = (a.entry_low + a.entry_high) / 2
    risk_a, risk_b = abs(entry - a.stop), abs(entry - b.stop)
    larger = max(risk_a, risk_b)
    if larger > 0 and abs(risk_a - risk_b) / larger > STOP_DIVERGENCE:
        return ConsensusResult(
            Agreement.DIVERGENT,
            False,
            f"same direction but incompatible risk: stops {a.stop} vs {b.stop}",
            plan=a,
        )
    # The primary's plan is the one that trades: merging two sets of levels would produce a plan
    # neither model actually proposed, and nobody could explain it afterwards.
    return ConsensusResult(Agreement.AGREE, True, f"both {a.side}, comparable risk", plan=a)
