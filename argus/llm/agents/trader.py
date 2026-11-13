"""The Trader agent: one asset's dossier in, a validated plan or an explicit NO_TRADE out.

Per asset rather than per portfolio (LLM_AGENTS.md §3.5 describes a batched Trader that picks at
most two plans across the universe). The Manager already loops per asset, and the portfolio-level
cap it describes is enforced downstream by the Risk Gate's entry limits — which is code, and
therefore the place that cap belongs.
"""

from __future__ import annotations

from argus.features.dossier import Dossier
from argus.features.dossier import render as render_dossier
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.gate.rules import FactorScores as GateScores
from argus.gate.rules import MarketContext, PlanInput
from argus.gate.rules import Target as GateTarget
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import TradePlan, TraderAnswer

AGENT = "trader"
PROMPT = "trader"


def rr_values(limits: DecisionLimits) -> dict[str, str]:
    """The R:R bars as the Trader prompts name them, from the account's profile.

    The numbers stay in the prompt on purpose (F-091/F-099: without them the Trader proposed plans
    the gate could never approve); only which numbers depends on the venue. Formatted so the live
    profile renders today's text exactly: "1.5R", "2R", "2.0".
    """
    return {
        "min_rr_tp1": f"{limits.min_rr_tp1:.1f}",
        "min_rr_blended": f"{limits.min_rr_blended:g}",
        "min_rr_blended_dp": f"{limits.min_rr_blended:.1f}",
    }


LIVE_STANCE_BATCH = """\
- If nothing clears the bar, return **no plans** and a `no_trade_reason`. That is a good outcome
  and a common one — most assets on most days are not trades, and an empty answer costs nothing
  while a weak plan costs money."""
DEMO_STANCE_BATCH = """\
- This is the DEMO account, run to explore. If any candidate has positive net evidence and a
  structural stop, return your best ones as plans — express low conviction in the scores, not by
  withholding the plan. Regime multipliers size a trade; they are not a reason to decline. Open
  positions and risk capacity are the Risk Gate's job, not yours. Return **no plans** and a
  `no_trade_reason` only when no candidate has a thesis at all. On this account code moves a
  target that sits too close out to the R:R floor, so do not drop a setup because the nearest
  level is close — give the most plausible structural target."""
LIVE_STANCE_SINGLE = """\
- If nothing clears the bar, return a NoTrade with the reason. That is a good outcome, not a
  failure — most assets on most days are not trades."""
DEMO_STANCE_SINGLE = """\
- This is the DEMO account, run to explore. If the asset has positive net evidence and a
  structural stop, return a plan — express low conviction in the scores, not by withholding the
  plan. Regime multipliers size a trade; they are not a reason to decline, and risk capacity is
  the Risk Gate's job. Return a NoTrade only when there is no thesis at all. On this account
  code moves a target that sits too close out to the R:R floor, so do not decline because the
  nearest level is close."""


def stance(limits: DecisionLimits, *, batch: bool) -> str:
    """The prompts' closing no-trade paragraph. Live renders the old text word for word."""
    if batch:
        return DEMO_STANCE_BATCH if limits.explore else LIVE_STANCE_BATCH
    return DEMO_STANCE_SINGLE if limits.explore else LIVE_STANCE_SINGLE


def build_prompt(
    dossier: Dossier,
    lessons: str = "-",
    traps: str = "-",
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> str:
    return render(
        PROMPT,
        dossier=render_dossier(dossier),
        regime=f"{dossier.regime.label} (risk x{dossier.regime.risk_mult})",
        lessons=lessons,
        traps=traps,
        no_trade_stance=stance(limits, batch=False),
        **rr_values(limits),
    )


def propose(
    router: Router,
    run_id: str,
    dossier: Dossier,
    market: MarketContext,
    used_families: dict[str, str] | None = None,
    lessons: str = "-",
    traps: str = "-",
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> CallResult[TraderAnswer]:
    """Ask for a plan. A NoTrade is a real answer, not an error, and is journaled as one."""
    prompt = build_prompt(dossier, lessons, traps, limits)
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=prompt,
        output_type=TraderAnswer,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )


def to_plan_input(plan: TradePlan) -> PlanInput:
    """Translate the LLM's contract into the gate's. Deliberately lossy in one direction only.

    The gate's input carries no thesis, no evidence and no free text — nothing the model wrote can
    influence sizing, because none of it survives this boundary.
    """
    return PlanInput(
        symbol=plan.symbol,
        side=plan.side,
        entry_kind=plan.entry_kind,
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        stop=plan.stop,
        targets=[GateTarget(t.price, t.close_pct) for t in plan.targets],
        time_stop_hours=plan.time_stop_hours,
        scores=GateScores(
            fundamental=plan.scores.fundamental,
            onchain=plan.scores.onchain,
            macro=plan.scores.macro,
            catalyst=plan.scores.catalyst,
            technical=plan.scores.technical,
        ),
        event_thesis=plan.event_thesis,
        urgent=plan.urgent,
        # The one piece of free text that crosses, and only to be written down: no gate reads
        # `thesis`, and `test_no_gate_rule_reads_the_thesis` keeps it that way.
        thesis=plan.thesis,
        # Validated `InvalidationRule`s, handed over as the plain dicts `killswitch.parse_rule`
        # reads and the journal stores.
        invalidation_rules=[r.model_dump(exclude_none=True) for r in plan.invalidation_rules],
    )
