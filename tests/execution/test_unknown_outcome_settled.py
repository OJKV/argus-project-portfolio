"""Review 2026-10-07 (exec): an entry left NEW after an unknown outcome was settled by a guess.

X-N5 leaves a placement whose outcome is unknown as NEW and says the resting sweep will settle
it. But for a row with no venue id the sweep guessed instead of asking: a market entry that timed
out, filled, and was stopped out within the grace hour was journalled REJECTED, "never reached the
venue", with no fills; with the position still open, the sweep said "check by hand" on every pass
and never resolved it. Every row carries its client order id, and the venue answers by it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import order_state, record_order
from argus.execution.resting import sweep
from argus.journal.db import open_journal
from tests.execution.test_resting import ENTRY, SYMBOL, _hours, _venue_id


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_journal(tmp_path / "journal.db")
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES ('r1','manager','demo','2026-10-06T00:00:00+00:00','OK',0,'demo')"
            )
        )
    return eng


def _timed_out_market_entry(engine: Engine, venue: FakeExchange) -> str:
    """Journalled NEW, then accepted by the venue with the reply lost (no id on the row)."""
    order = {
        "symbol": SYMBOL,
        "side": "buy",
        "size": Decimal("1"),
        "order_type": "market",
        "cl_ord_id": "argusunknown1",
        "attach_algos": [{"kind": "sl", "trigger_px": ENTRY * Decimal("0.97")}],
    }
    order_id = record_order(engine, "r1", order)
    venue.place_bracket(order)
    return order_id


def _row(engine: Engine, order_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM orders WHERE order_id = :o"), {"o": order_id}
        ).mappings()
        return dict(row.one())


def _fills(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM fills")).scalar_one())


def test_a_filled_and_stopped_out_entry_left_new_is_settled_filled(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _timed_out_market_entry(engine, venue)
    venue.fill(_venue_id(venue))
    venue.positions.clear()  # its attached stop fired inside the grace hour

    result = sweep(engine, venue, now=_hours(2), valid_hours=12, drift_pct=2.0, venue="demo")

    assert result.errors == []
    assert order_state(engine, order_id) == "FILLED"
    assert _fills(engine) == 1
    assert _row(engine, order_id)["exchange_id"] == _venue_id(venue)


def test_a_filled_entry_left_new_with_its_position_open_is_resolved(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _timed_out_market_entry(engine, venue)
    venue.fill(_venue_id(venue))

    result = sweep(engine, venue, now=_hours(2), valid_hours=12, drift_pct=2.0, venue="demo")

    assert result.errors == []
    assert order_state(engine, order_id) == "FILLED"


def test_an_entry_the_venue_never_saw_is_still_rejected(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _timed_out_market_entry(engine, venue)
    venue.orders.clear()

    sweep(engine, venue, now=_hours(2), valid_hours=12, drift_pct=2.0, venue="demo")

    assert order_state(engine, order_id) == "REJECTED"
