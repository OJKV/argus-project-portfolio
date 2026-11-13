"""Positions as the venue showed them, written down every time we look.

`positions_snapshots` existed from the first migration and **nothing ever wrote it** (F-132). The
watchdog fetched positions every twenty minutes to check each had a stop, then discarded them, so
the system had no memory of what it held — the dashboard could not show a position, and nothing
could tell that one had closed.

That second consequence is the expensive one. A position leaves the venue silently: a stop fills,
the row simply stops appearing, and no event is raised anywhere. Comparing the last snapshot with
the current fetch is the only way to notice, which makes this table the thing that decides whether
a closed trade ever reaches the Post-mortem and then the Reflector.

Insert-only, like every other ledger here. A snapshot is what was true at a moment; correcting one
would erase the moment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import Engine, text

from argus.journal.db import new_id

if TYPE_CHECKING:
    from argus.exchange.base import ExchangeAdapter

log = structlog.get_logger(__name__)


def _f(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def record(
    engine: Engine,
    run_id: str,
    positions: list[dict[str, Any]],
    unprotected: set[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Write one row per open position. Returns how many were written."""
    if not positions:
        return 0
    naked = unprotected or set()
    stamp = (now or datetime.now(UTC)).isoformat()
    rows = [
        {
            "snapshot_id": new_id(),
            "run_id": run_id,
            "created_at": stamp,
            "symbol": p.get("symbol"),
            "side": p.get("side"),
            "size": str(p.get("size", "")),
            "entry_price": str(p.get("entry_price") or p.get("entry") or ""),
            "mark_price": str(p.get("mark_price") or p.get("mark") or ""),
            "leverage": str(p.get("leverage") or ""),
            "margin": str(p.get("margin") or ""),
            "unrealised": str(p.get("unrealised") or p.get("upl") or ""),
            "liq_price": str(p.get("liq_price") or ""),
            "state": "OPEN",
            # The venue's own answer to the only question the watchdog exists to ask.
            "protection": "NONE" if p.get("symbol") in naked else "SL_ATTACHED",
            "plan_id": p.get("plan_id"),
            "raw": json.dumps(p, default=str),
        }
        for p in positions
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO positions_snapshots (snapshot_id, run_id, created_at, symbol, side,"
                " size, entry_price, mark_price, leverage, margin, unrealised, liq_price, state,"
                " protection, plan_id, raw) VALUES (:snapshot_id, :run_id, :created_at, :symbol,"
                " :side, :size, :entry_price, :mark_price, :leverage, :margin, :unrealised,"
                " :liq_price, :state, :protection, :plan_id, :raw)"
            ),
            rows,
        )
    return len(rows)


def latest_open(engine: Engine, *, venue: str) -> dict[str, dict[str, Any]]:
    """The most recent snapshot on `venue` of each symbol that was open when last seen.

    One journal holds both accounts, and a run records which in `runs.config_hash` (F-131). Read
    across both, a live tick "closed" every demo position it could not see — fake post-mortems
    for the Reflector — and a newer demo row hid an open live one from the unreachable-venue
    check (F-149). So "latest" is the latest within the account.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT p.* FROM positions_snapshots p"
                    " JOIN runs r ON r.run_id = p.run_id"
                    " JOIN (SELECT s.symbol, MAX(s.created_at) AS t FROM positions_snapshots s"
                    "        JOIN runs sr ON sr.run_id = s.run_id"
                    "        WHERE sr.config_hash = :venue GROUP BY s.symbol) last"
                    "   ON last.symbol = p.symbol AND last.t = p.created_at"
                    " WHERE p.state = 'OPEN' AND r.config_hash = :venue"
                ),
                {"venue": venue},
            )
            .mappings()
            .all()
        )
    return {row["symbol"]: dict(row) for row in rows}


@dataclass(frozen=True)
class Closed:
    symbol: str
    last_seen: dict[str, Any]
    # When the CLOSED row was written, i.e. when someone noticed. Set by `closed_since` only: a
    # close that has just been detected has not been marked yet.
    closed_at: str | None = None

    @property
    def entry_price(self) -> float | None:
        return _f(self.last_seen.get("entry_price"))

    @property
    def side(self) -> str:
        return str(self.last_seen.get("side") or "long")


def newly_closed(engine: Engine, current: list[dict[str, Any]], *, venue: str) -> list[Closed]:
    """Symbols we had open on `venue` last time and the venue no longer reports.

    This is the only close signal the system has. A stop filling raises no event anywhere — the
    row just stops appearing — so a trade that is not noticed here is a trade the Reflector never
    learns from.
    """
    open_now = {p.get("symbol") for p in current}
    return [
        Closed(symbol=symbol, last_seen=row)
        for symbol, row in latest_open(engine, venue=venue).items()
        if symbol not in open_now
    ]


def closed_since(engine: Engine, since: datetime, *, venue: str) -> list[Closed]:
    """Every close on `venue` marked at or after `since`, oldest first, whoever noticed it.

    Scoped by the run that wrote the CLOSED row (F-149, F-150): one journal holds both accounts,
    and a demo review judging a live close would write a verdict about the wrong account.

    Each comes back as the open snapshot its CLOSED row closed from — the same `last_seen` that
    `newly_closed` hands out — so a reader judging the trade sees one shape whether the watchdog
    or a review marked it.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT o.*, c.created_at AS closed_at FROM positions_snapshots c"
                    " JOIN positions_snapshots o"
                    "   ON o.snapshot_id = json_extract(c.raw, '$.closed_from')"
                    " JOIN runs r ON r.run_id = c.run_id"
                    " WHERE c.state = 'CLOSED' AND c.created_at >= :since"
                    "   AND r.config_hash = :venue"
                    " ORDER BY c.created_at, c.rowid"
                ),
                {"since": since.isoformat(), "venue": venue},
            )
            .mappings()
            .all()
        )
    return [
        Closed(
            symbol=row["symbol"],
            last_seen={k: v for k, v in row.items() if k != "closed_at"},
            closed_at=row["closed_at"],
        )
        for row in rows
    ]


def venue_realised(venue: ExchangeAdapter, closed: list[Closed]) -> dict[str, Decimal | None]:
    """What the venue booked for each close since we last saw it open; None where it cannot say.

    The last snapshot is up to a watchdog tick old, so its unrealised PnL is not what the trade
    was worth: a long last seen at +0.1R can stop out at -1R before the next look. Asking never
    breaks the bookkeeping - a failed answer is None, which the reader books at the plan stop.
    """
    out: dict[str, Decimal | None] = {}
    for c in closed:
        try:
            seen = datetime.fromisoformat(str(c.last_seen.get("created_at")))
            since = seen if seen.tzinfo else seen.replace(tzinfo=UTC)
            out[c.symbol] = venue.fetch_realised_pnl(c.symbol, since)
        except Exception as exc:  # noqa: BLE001 - an unknown PnL is recorded as unknown
            log.warning("positions.realised_unknown", symbol=c.symbol, error=str(exc))
            out[c.symbol] = None
    return out


def mark_closed(
    engine: Engine,
    run_id: str,
    closed: list[Closed],
    now: datetime | None = None,
    realised: dict[str, Decimal | None] | None = None,
    resting: dict[str, list[str]] | None = None,
) -> None:
    """Append a CLOSED row so the same position is not reported closed twice.

    `realised` is the venue's booked PnL per symbol (`venue_realised`). It is written only when
    known: an absent figure is unknown, and a reader must never take it for zero.

    `resting` maps a closed symbol to the venue ids and client ids of the orders still resting on
    that asset when the close was noticed. This row is stamped when someone *notices* the close,
    up to a watchdog interval after the stop filled; an entry placed in that gap rested through
    it and is the next position, not part of this one (review 2026-10-07).
    """
    if not closed:
        return
    booked = realised or {}
    stamp = (now or datetime.now(UTC)).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO positions_snapshots (snapshot_id, run_id, created_at, symbol, side,"
                " size, entry_price, mark_price, leverage, margin, unrealised, state, protection,"
                " raw) VALUES (:snapshot_id, :run_id, :created_at, :symbol, :side, '0',"
                " :entry_price, :mark_price, '', '', '', 'CLOSED', 'EXITED', :raw)"
            ),
            [
                {
                    "snapshot_id": new_id(),
                    "run_id": run_id,
                    "created_at": stamp,
                    "symbol": c.symbol,
                    "side": c.side,
                    # Carried from the last open snapshot: the closing row is where a reader
                    # looks to ask what the trade was, and an empty one answers nothing.
                    "entry_price": c.last_seen.get("entry_price") or "",
                    "mark_price": c.last_seen.get("mark_price") or "",
                    "raw": json.dumps(
                        {
                            "closed_from": c.last_seen.get("snapshot_id"),
                            "resting": sorted((resting or {}).get(c.symbol, [])),
                        }
                        | (
                            {}
                            if booked.get(c.symbol) is None
                            else {"realised_pnl": str(booked[c.symbol])}
                        )
                    ),
                }
                for c in closed
            ],
        )
