"""Position snapshots, and the close nobody else would notice."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.journal import positions
from argus.journal.db import open_journal

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A journal with the runs these snapshots hang off.

    `positions_snapshots.run_id` is a foreign key, which is the point: a snapshot belonging to no
    run is a reading nobody can date or attribute.
    """
    eng = open_journal(tmp_path / "journal.db")
    with eng.begin() as conn:
        for run in ("r1", "r2"):
            conn.execute(
                text(
                    "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                    " VALUES (:r,'watchdog','demo',:t,'OK','demo')"
                ),
                {"r": run, "t": NOW.isoformat()},
            )
    return eng


def _pos(symbol: str, **over: Any) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": "long",
        "size": "3",
        "entry_price": 100.0,
        "mark_price": 104.0,
        "leverage": 2,
        "unrealised": "12.0",
    } | over


def test_a_snapshot_is_written_per_open_position(engine: Engine) -> None:
    written = positions.record(engine, "r1", [_pos("SOL"), _pos("ETH")], now=NOW)
    assert written == 2
    assert set(positions.latest_open(engine, venue="demo")) == {"SOL", "ETH"}


def test_an_unprotected_position_is_recorded_as_such(engine: Engine) -> None:
    """The one question the watchdog exists to ask, written down rather than only logged."""
    positions.record(engine, "r1", [_pos("SOL"), _pos("ETH")], unprotected={"SOL"}, now=NOW)
    rows = positions.latest_open(engine, venue="demo")
    assert rows["SOL"]["protection"] == "NONE"
    assert rows["ETH"]["protection"] == "SL_ATTACHED"


def test_a_position_that_stops_appearing_is_a_close(engine: Engine) -> None:
    """A stop filling raises no event anywhere — the row simply stops appearing.

    This comparison is the system's ONLY close signal, and a close nobody notices is a trade the
    Reflector never learns from (F-132).
    """
    positions.record(engine, "r1", [_pos("SOL"), _pos("ETH")], now=NOW)
    closed = positions.newly_closed(engine, [_pos("ETH")], venue="demo")

    assert [c.symbol for c in closed] == ["SOL"]
    assert closed[0].entry_price == 100.0
    assert closed[0].side == "long"


def test_a_close_is_reported_once(engine: Engine) -> None:
    positions.record(engine, "r1", [_pos("SOL")], now=NOW)
    closed = positions.newly_closed(engine, [], venue="demo")
    assert [c.symbol for c in closed] == ["SOL"]

    positions.mark_closed(engine, "r1", closed, now=NOW + timedelta(minutes=20))
    # Without this the same trade would be post-mortemed on every tick forever.
    assert positions.newly_closed(engine, [], venue="demo") == []


def test_reopening_a_symbol_is_visible_again(engine: Engine) -> None:
    positions.record(engine, "r1", [_pos("SOL")], now=NOW)
    positions.mark_closed(engine, "r1", positions.newly_closed(engine, [], venue="demo"), now=NOW)

    positions.record(engine, "r2", [_pos("SOL", entry_price=90.0)], now=NOW + timedelta(hours=2))
    assert "SOL" in positions.latest_open(engine, venue="demo")
    assert positions.newly_closed(engine, [], venue="demo")[0].entry_price == 90.0


def test_nothing_open_writes_nothing(engine: Engine) -> None:
    assert positions.record(engine, "r1", [], now=NOW) == 0
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM positions_snapshots")).scalar() == 0


def test_snapshots_are_insert_only(engine: Engine) -> None:
    """A snapshot is what was true at a moment; correcting one would erase the moment."""
    positions.record(engine, "r1", [_pos("SOL")], now=NOW)
    positions.record(engine, "r1", [_pos("SOL", mark_price=108.0)], now=NOW + timedelta(minutes=20))
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT mark_price FROM positions_snapshots WHERE symbol='SOL' ORDER BY created_at"
            )
        ).all()
    assert [r[0] for r in rows] == ["104.0", "108.0"]


def test_a_close_carries_the_realised_pnl_the_venue_reported(engine: Engine) -> None:
    positions.record(engine, "r1", [_pos("SOL"), _pos("ETH")], now=NOW)
    closed = positions.newly_closed(engine, [], venue="demo")

    positions.mark_closed(
        engine, "r2", closed, now=NOW, realised={"SOL": Decimal("-40.5"), "ETH": None}
    )

    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text("SELECT symbol, raw FROM positions_snapshots WHERE state = 'CLOSED'")
            ).all()
        )
    assert json.loads(rows["SOL"])["realised_pnl"] == "-40.5"
    assert "realised_pnl" not in json.loads(rows["ETH"])  # unknown is absent, never zero


def test_asking_the_venue_never_breaks_the_close_bookkeeping(engine: Engine) -> None:
    from argus.exchange.fake import FakeExchange

    positions.record(engine, "r1", [_pos("SOL"), _pos("ETH")], now=NOW)
    closed = positions.newly_closed(engine, [], venue="demo")
    venue = FakeExchange(realised={"SOL": Decimal("-7")})
    asked: list[tuple[str, datetime]] = []

    def answer(symbol: str, since: datetime) -> Decimal | None:
        asked.append((symbol, since))
        if symbol == "ETH":
            raise TimeoutError("positions-history timed out")
        return venue.realised.get(symbol)

    venue.fetch_realised_pnl = answer  # type: ignore[method-assign]

    assert positions.venue_realised(venue, closed) == {"SOL": Decimal("-7"), "ETH": None}
    assert {since for _, since in asked} == {NOW}  # asked from the last time we saw it open
