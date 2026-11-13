"""Closed trades, assembled from the ledger so the Reflector reads history rather than opinion.

A "trade" is not a table here. It is a plan that opened a position, the snapshots that recorded
the position open and then gone, and the post-mortem written when it closed — three sources joined
at read time, because each was written once by whoever knew it and none of them can be edited
afterwards.

`store_postmortem` is the only writer. It records the verdict *and* the computed flags it was
formed against, so a later reader can ask whether the judgement matched the arithmetic without
re-deriving numbers from a market that has since moved on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text

from argus.llm.schemas import PostMortem

MAX_WINDOW = 30  # LLM_AGENTS.md §3.7


def store_postmortem(
    engine: Engine,
    run_id: str,
    plan_id: str,
    symbol: str,
    report: PostMortem,
    outcome: dict[str, Any],
    flags: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO postmortems (plan_id, created_at, run_id, symbol,"
                " outcome_r, exit_reason, process_verdict, would_take_again, entry_timing,"
                " sizing, stop_placement, flags, report) VALUES (:plan_id, :created_at, :run_id,"
                " :symbol, :outcome_r, :exit_reason, :process_verdict, :would_take_again,"
                " :entry_timing, :sizing, :stop_placement, :flags, :report)"
            ),
            {
                "plan_id": plan_id,
                "created_at": (now or datetime.now(UTC)).isoformat(),
                "run_id": run_id,
                "symbol": symbol,
                "outcome_r": outcome.get("outcome_r"),
                "exit_reason": outcome.get("exit_reason"),
                "process_verdict": report.process_verdict,
                "would_take_again": int(report.would_take_again),
                "entry_timing": report.entry_timing,
                "sizing": report.sizing,
                "stop_placement": report.stop_placement,
                "flags": json.dumps(flags or {}, default=str),
                "report": report.model_dump_json(),
            },
        )


# Sorts before any ISO-8601 stamp the journal holds: "every close", with no lookback.
_EPOCH = datetime.min.replace(tzinfo=UTC)


def closed_trades(engine: Engine, limit: int = MAX_WINDOW, *, venue: str) -> list[dict[str, Any]]:
    """The most recent closed trades on `venue`, oldest first, in the shape the Reflector expects.

    A trade is closed when the watchdog or a review wrote a CLOSED `positions_snapshots` row for
    it (`positions.closed_since`) - the only close signal the system has, since a stop filling
    raises no event anywhere (F-132). It used to select a `position_events` row that nothing
    writes, so the window was always empty and the Reflector could never learn from a real trade
    (F-152). The plan behind the close is found exactly as the post-mortem found it
    (`review.plan_behind`), so a verdict and the trade it judged cannot come apart.

    Trades without a post-mortem are still included: the Reflector can read an outcome without a
    verdict, and silently dropping them would quietly narrow the window it believes it is
    reasoning over. A close with no plan behind it - a position this system did not open - is
    not a trade it can learn from, and a close marked twice is one trade.

    Scoped to one account (F-149, F-150): one journal holds demo and live, and a lesson learned
    from the other account's trades would move prompts it never ran under. Read-only.
    """
    from argus.journal.positions import closed_since
    from argus.runs.review import plan_behind

    if limit <= 0:
        return []
    chosen: list[tuple[str, str | None, dict[str, Any]]] = []
    seen: set[str] = set()
    # Newest first, so the window fills from the recent end and stops as soon as it is full.
    for position in reversed(closed_since(engine, _EPOCH, venue=venue)):
        placed = plan_behind(engine, position, venue=venue)
        if placed.plan_id is None or placed.plan_id in seen:
            continue
        seen.add(placed.plan_id)
        chosen.append((placed.plan_id, position.closed_at, placed.plan))
        if len(chosen) == limit:
            break

    trades: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for plan_id, closed_at, plan in reversed(chosen):  # oldest first: a sequence, not a set
            row = (
                conn.execute(
                    text(
                        "SELECT p.symbol, p.side, p.created_at,"
                        "       pm.outcome_r, pm.exit_reason, pm.process_verdict,"
                        "       pm.would_take_again, pm.entry_timing, pm.stop_placement, pm.flags"
                        " FROM plans p LEFT JOIN postmortems pm ON pm.plan_id = p.plan_id"
                        " WHERE p.plan_id = :plan_id"
                    ),
                    {"plan_id": plan_id},
                )
                .mappings()
                .one()
            )
            flags = json.loads(row["flags"]) if row["flags"] else {}
            trade: dict[str, Any] = {
                "trade_id": plan_id[:8],
                "symbol": row["symbol"],
                "side": row["side"],
                "opened_at": row["created_at"],
                "closed_at": closed_at,
                "exit_reason": row["exit_reason"],
                "thesis": plan.get("thesis"),
                "regime_at_entry": (plan.get("market") or {}).get("regime"),
                "outcome_r": row["outcome_r"],
            }
            if flags:
                trade["entry_slippage_pct"] = flags.get("entry_slippage_pct")
                trade["stop_distance_atr"] = flags.get("stop_distance_atr")
            if row["process_verdict"]:
                trade["postmortem"] = {
                    "process_verdict": row["process_verdict"],
                    "would_take_again": bool(row["would_take_again"]),
                    "entry_timing": row["entry_timing"],
                    "stop_placement": row["stop_placement"],
                }
            trades.append(trade)
    return trades
