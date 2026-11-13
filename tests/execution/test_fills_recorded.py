"""Review 2026-10-07 X-N11: fills were never recorded.

`lifecycle.apply_fill` had no production caller, and the resting sweep marked an entry FILLED with
a bare state transition — no `fills` row, no price, no fee. Realised PnL, fees and R therefore
rested on position snapshots, and the fee-viability check (F-026) had no realised fees to compare
against.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import order_state
from argus.execution.resting import sweep
from argus.journal import ledger
from argus.journal.db import open_journal
from tests.execution.test_resting import ENTRY, SYMBOL, _hours, _resting, _venue_id


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


def _fills(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text("SELECT * FROM fills")).mappings()]


def test_a_filled_entry_gets_a_fill_row_with_the_venue_price_and_fee(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)
    venue.fill(_venue_id(venue), price=Decimal("74990"))

    sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")

    assert order_state(engine, order_id) == "FILLED"
    fills = _fills(engine)
    assert len(fills) == 1
    fill = fills[0]
    assert fill["order_id"] == order_id
    assert Decimal(fill["price"]) == Decimal("74990")
    assert Decimal(fill["size"]) == Decimal("1")
    # Signed as the venue reports it: a charge is negative (F-036).
    assert Decimal(fill["fee"]) < 0
    assert fill["fee_ccy"] == "USDT"
    assert ledger.verify(engine, "fills")[0]


def test_a_second_sweep_does_not_record_the_fill_twice(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    _resting(engine, venue)
    venue.fill(_venue_id(venue))
    sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")
    sweep(engine, venue, now=_hours(2), valid_hours=12, drift_pct=2.0, venue="demo")
    assert len(_fills(engine)) == 1


def test_a_fill_with_no_reported_fee_is_not_invented(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero is not a reading (F-030): with no fee from the venue, no fill row is written."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)
    venue.fill(_venue_id(venue))
    real = venue.fetch_order

    def no_fee(symbol: str, oid: str) -> dict[str, Any]:
        row = real(symbol, oid)
        row.pop("fee", None)
        row.pop("info", None)
        return row

    monkeypatch.setattr(venue, "fetch_order", no_fee)
    sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")
    assert order_state(engine, order_id) == "FILLED"
    assert _fills(engine) == []
