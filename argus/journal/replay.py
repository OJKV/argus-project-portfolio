"""Replay a finished run from the journal — no network, no LLM, no side effects.

This is the first tool to reach for when something goes wrong in production (CLAUDE.md, "When
something fails in production"): it answers *what did the run see, and what did it decide* from
rows that were written once and hash-chained, rather than from a re-fetch that would show today's
market instead of the one the run traded.

The gate's inputs are journaled with every plan (`payload.market`), so a rejection can be
explained offline. Re-running `evaluate()` against those inputs to diff old verdicts against new
code is the Phase 4 extension; it needs the LLM proposer to be deterministic first.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text


def _loads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


@dataclass
class Decision:
    symbol: str
    side: str | None
    state: str
    step: int | None
    verdict: str
    plan: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)

    @property
    def rejections(self) -> list[dict[str, Any]]:
        reasons = self.gate.get("reasons") or []
        return [r for r in reasons if r.get("verdict") == "REJECT"]

    @property
    def market(self) -> dict[str, Any]:
        return self.plan.get("market") or {}


@dataclass
class RunReplay:
    run: dict[str, Any]
    regime: dict[str, Any] | None
    dossiers: list[dict[str, Any]]
    decisions: list[Decision]
    # Status -> count and model -> count over the run's LLM calls. Without them, "1 plan from
    # 15 assets" is ambiguous: considered refusals and silent failures look identical from
    # the gate's side and call for opposite responses.
    llm_calls: dict[str, int] = field(default_factory=dict)
    llm_models: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        tally = {"APPROVED": 0, "ADJUSTED": 0, "REJECTED": 0}
        for decision in self.decisions:
            if decision.verdict in tally:
                tally[decision.verdict] += 1
        return tally


class RunNotFoundError(LookupError):
    pass


def last_run_id(engine: Engine, kind: str = "manager") -> str | None:
    """The most recent run of a kind — so "what just happened?" needs no copied id."""
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT run_id FROM runs WHERE kind = :k ORDER BY started_at DESC LIMIT 1"),
            {"k": kind},
        ).scalar()


def load(engine: Engine, run_id: str) -> RunReplay:
    with engine.connect() as conn:
        run = (
            conn.execute(text("SELECT * FROM runs WHERE run_id = :r"), {"r": run_id})
            .mappings()
            .first()
        )
        if run is None:
            raise RunNotFoundError(run_id)
        regime = (
            conn.execute(
                text(
                    "SELECT * FROM regime_snapshots WHERE run_id = :r ORDER BY created_at LIMIT 1"
                ),
                {"r": run_id},
            )
            .mappings()
            .first()
        )
        dossiers = (
            conn.execute(
                text(
                    "SELECT symbol, source, tokens, trade_critical, gaps FROM dossiers"
                    " WHERE run_id = :r ORDER BY created_at, symbol"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )
        rows = (
            conn.execute(
                text(
                    "SELECT p.symbol, p.side, p.state, p.step, p.payload, g.verdict, g.reasons"
                    " FROM plans p LEFT JOIN gate_results g ON g.plan_id = p.plan_id"
                    " WHERE p.run_id = :r ORDER BY p.created_at, p.symbol"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )

    with engine.connect() as conn:
        llm = conn.execute(
            text(
                "SELECT status, provider || '/' || model AS label FROM llm_calls WHERE run_id = :r"
            ),
            {"r": run_id},
        ).all()
    llm_status = Counter(row[0] for row in llm)
    llm_models = Counter(row[1] for row in llm)

    snapshot = None
    if regime is not None:
        snapshot = dict(regime)
        for key in ("inputs", "reasons", "modifiers", "gaps"):
            snapshot[key] = _loads(snapshot.get(key))

    decisions = [
        Decision(
            symbol=row["symbol"],
            side=row["side"],
            state=row["state"],
            step=row["step"],
            verdict=row["verdict"] or "-",
            plan=_loads(row["payload"]) or {},
            gate=_loads(row["reasons"]) or {},
        )
        for row in rows
    ]
    return RunReplay(
        llm_calls=llm_status,
        llm_models=llm_models,
        run=dict(run),
        regime=snapshot,
        dossiers=[dict(d) | {"gaps": _loads(d["gaps"]) or []} for d in dossiers],
        decisions=decisions,
    )


def as_dict(replay: RunReplay) -> dict[str, Any]:
    return {
        "run": replay.run,
        "regime": replay.regime,
        "dossiers": replay.dossiers,
        "counts": replay.counts,
        "llm_calls": dict(replay.llm_calls),
        "llm_models": dict(replay.llm_models),
        "decisions": [
            {
                "symbol": d.symbol,
                "side": d.side,
                "state": d.state,
                "step": d.step,
                "verdict": d.verdict,
                "rejections": d.rejections,
                "market": d.market,
                "plan": d.plan,
            }
            for d in replay.decisions
        ],
    }
