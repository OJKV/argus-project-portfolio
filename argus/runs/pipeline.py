"""The adversarial chain that stands between a Trader's plan and the Risk Gate.

Until now a plan went from the Trader straight to the gate. The gate is code and catches what
arithmetic can catch — stop inside structure, R-multiple, heat, correlation — but nothing asked a
second model whether the trade made sense, and nothing weighed it against the rest of the book.
Both of those agents existed and were measured; neither was called.

Three checks, in this order, and the order matters:

1. **Consensus** answers the same question on a different family without seeing the first answer.
   It is a veto, not a vote: a trade needs both to want it, so a refusal here drops the plan.
2. **Red Team** attacks the plan as a reading — uncited scores, ignored contradictions, invented
   levels. It is read by **severity**, never by `agree`, which was measured near-constant False on
   sound readings (F-136): below 3 nothing changes, 3 halves the trade, 4–5 drop it. It runs before
   the Risk Debate because a reading that cannot be trusted is not worth weighing against the book.

On the demo account the exploration profile (`argus/gate/limits.py`, DECISION_LOGIC §11) relaxes
three of these: severity 4 halves instead of dropping, a consensus *outage* proceeds (a real
disagreement still vetoes), and a Risk Debate `decline` halves like `reduce`. Live is unchanged.
3. **Risk Debate** then judges the surviving plan against the open book. It cannot ask for more —
   `proceed`, `reduce` and `decline` map to 1.0, 0.5 and 0.0, clamped.

The factors multiply, so a halved red-team verdict and a `reduce` compound to a quarter.

Both are `safety` criticality, which decides what a failure *means*: silence from an agent whose
job is to object is not approval, so a failed check drops the trade rather than skipping it. That
is the opposite of `optional`, where the run carries on and only the log notices.

Sizing still belongs to `argus/gate/sizing.py`. What comes back here is a *factor* the gate
applies, never a size, and it can only ever shrink.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from argus.features.dossier import Dossier
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.journal.db import new_id, utc_now
from argus.llm.agents import consensus as consensus_agent
from argus.llm.agents import redteam as red_agent
from argus.llm.agents import riskdebate as risk_agent
from argus.llm.evidence import unsupported_scores
from argus.llm.router import AllModelsFailedError, Router
from argus.llm.schemas import TraderAnswer


@dataclass
class Vetting:
    """What the chain decided, and enough of why to journal it."""

    size_factor: float = 1.0
    blocked_by: str | None = None
    checks: list[tuple[str, str]] = field(default_factory=list)
    # factor -> the paths it cited, for every score the evidence check refused
    unsupported: dict[str, list[str]] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.blocked_by is None and self.size_factor > 0.0

    def record(self, agent: str, outcome: str) -> None:
        self.checks.append((agent, outcome))


def vet(
    router: Router,
    run_id: str,
    dossier: Dossier,
    answer: TraderAnswer,
    trader_family: str,
    portfolio: dict[str, Any],
    regime: str,
    calendar: list[dict[str, Any]] | None = None,
    lessons: str = "-",
    journal: Engine | None = None,
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> Vetting:
    """Run the second opinion and the risk debate over one proposed plan.

    With `journal`, the outcome is written to `vettings` whichever way it went — a block
    included, because a plan the chain drops never reaches `plans` and would otherwise leave
    nothing behind but a console line.
    """
    result = _vet(
        router, run_id, dossier, answer, trader_family, portfolio, regime, calendar, lessons, limits
    )
    if journal is not None:
        _record(journal, run_id, dossier.symbol, answer, trader_family, result)
    return result


def _record(
    engine: Engine,
    run_id: str,
    symbol: str,
    answer: TraderAnswer,
    trader_family: str,
    result: Vetting,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vettings (vetting_id, run_id, created_at, symbol, trader_family,"
                " blocked_by, size_factor, checks, unsupported, plan)"
                " VALUES (:i, :r, :t, :s, :f, :b, :z, :c, :u, :p)"
            ),
            {
                "i": new_id(),
                "r": run_id,
                "t": utc_now(),
                "s": symbol,
                "f": trader_family,
                "b": result.blocked_by,
                "z": result.size_factor if result.approved else 0.0,
                "c": json.dumps(result.checks),
                "u": json.dumps(result.unsupported),
                "p": None if answer.plan is None else answer.plan.model_dump_json(),
            },
        )


def _vet(
    router: Router,
    run_id: str,
    dossier: Dossier,
    answer: TraderAnswer,
    trader_family: str,
    portfolio: dict[str, Any],
    regime: str,
    calendar: list[dict[str, Any]] | None,
    lessons: str,
    limits: DecisionLimits,
) -> Vetting:
    result = Vetting()
    if answer.plan is None:
        result.blocked_by = "trader"
        result.record("trader", "no_trade")
        return result

    # Before any agent is asked: a score whose every citation names a field the dossier does not
    # have is an uncited score, which the schema refuses — but only code holding the dossier can
    # see it (L-10, non-negotiable 6). It used to be checked only by the golden scorer.
    unsupported = unsupported_scores(answer.plan, dossier)
    if unsupported:
        result.blocked_by = "evidence"
        result.unsupported = unsupported
        result.record("evidence", f"no real citation for {', '.join(sorted(unsupported))}")
        return result

    try:
        second = consensus_agent.second_opinion(
            router, run_id, dossier, trader_family, lessons=lessons, limits=limits
        )
    except AllModelsFailedError as exc:
        # Safety criticality: no second opinion means no trade. Trading anyway would quietly
        # convert an unavailable check into a passed one, which is the failure mode the
        # criticality field exists to prevent. The demo exploration profile (Q5) accepts exactly
        # that on the demo account: an outage proceeds, a real disagreement below still vetoes.
        result.record("consensus", f"unavailable: {type(exc).__name__}")
        if not limits.consensus_outage_proceeds:
            result.blocked_by = "consensus"
            return result
        result.record("consensus", f"outage proceeds ({limits.name})")
        second = None

    consensus_factor = 1.0
    if second is not None:
        agreement = consensus_agent.compare(answer, second.output)
        result.record("consensus", agreement.agreement.value)
        one_sided = agreement.agreement is consensus_agent.Agreement.ONE_SIDED
        if one_sided and limits.consensus_one_sided_factor > 0.0:
            # Demo (owner 2026-10-26): a second opinion that only declines halves the size; a
            # disagreement on side or stop still vetoes below. Live keeps the veto.
            consensus_factor = limits.consensus_one_sided_factor
            result.record("consensus", f"one_sided proceeds x{consensus_factor:g} ({limits.name})")
        elif not agreement.tradeable:
            result.blocked_by = "consensus"
            return result

    plan_payload = answer.plan.model_dump(mode="json")
    try:
        attack = red_agent.challenge_plan(router, run_id, dossier, plan_payload, trader_family)
    except AllModelsFailedError as exc:
        result.blocked_by = "redteam"
        result.record("redteam", f"unavailable: {type(exc).__name__}")
        return result

    red_factor = red_agent.size_factor(attack.output, limits)
    result.record("redteam", f"severity {attack.output.severity}/5 -> x{red_factor:g}")
    if red_factor <= 0.0:
        result.size_factor = 0.0
        result.blocked_by = "redteam"
        return result

    try:
        verdict = risk_agent.debate(
            router, run_id, plan_payload, portfolio, regime, calendar=calendar, limits=limits
        )
    except AllModelsFailedError as exc:
        result.blocked_by = "riskdebate"
        result.record("riskdebate", f"unavailable: {type(exc).__name__}")
        return result

    result.record("riskdebate", verdict.output.recommendation)
    result.size_factor = (
        consensus_factor * red_factor * risk_agent.apply_verdict(verdict.output, limits)
    )
    if result.size_factor <= 0.0:
        result.blocked_by = "riskdebate"
    return result
