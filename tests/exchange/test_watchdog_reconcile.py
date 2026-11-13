"""Review 2026-10-07 X-N2: `exchange/reconcile.py` (ghost / orphan / drift) was never called.

The watchdog used the adapter's `reconcile()`, which only reports positions without a stop. A
position OKX accepted after a placement timeout — journalled REJECTED (X-N5) — lived with no
plan, was never surfaced, was reviewed against an empty plan and skipped by the post-mortem.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import record_order, transition_order
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog, WatchdogResult

BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _protected(ex: FakeExchange, symbol: str, side: str = "long") -> None:
    ex.marks.setdefault(symbol, Decimal("3000"))
    ex.open_unprotected(symbol, side=side)
    entry = ex.positions[symbol].entry_price
    ex.amend_stop(symbol, entry * (Decimal("0.9") if side == "long" else Decimal("1.1")))


def _entry(engine: Engine, symbol: str, side: str, *states: str) -> str:
    with start_run(engine, "manager", "demo") as run_id:
        order_id = record_order(
            engine,
            run_id,
            {
                "symbol": symbol,
                "side": side,
                "size": Decimal("1"),
                "order_type": "market",
                "cl_ord_id": f"cl{symbol[:3]}{side}{len(states)}",
            },
        )
        for state in states:
            transition_order(engine, order_id, state, "test")
    return order_id


def _tick(ex: FakeExchange, engine: Engine, tmp_path: Path, bot: RecordingBot) -> WatchdogResult:
    with start_run(engine, "watchdog", "demo") as run_id:
        return Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=bot, chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
        ).run_once(run_id)


def _orphan_alerts(bot: RecordingBot) -> list[str]:
    return [t for _, t in bot.sent if "orphan" in t.lower()]


def test_a_position_the_journal_cannot_explain_is_alerted_once(
    engine: Engine, tmp_path: Path
) -> None:
    ex = FakeExchange()
    _protected(ex, BTC)
    bot = RecordingBot()

    first = _tick(ex, engine, tmp_path, bot)
    assert any("orphan" in d for d in first.discrepancies)
    assert len(_orphan_alerts(bot)) == 1

    _tick(ex, engine, tmp_path, bot)
    assert len(_orphan_alerts(bot)) == 1, "the same orphan must not page every 20 minutes"


def test_the_x_n5_case_a_rejected_entry_that_is_live_at_the_venue(
    engine: Engine, tmp_path: Path
) -> None:
    _entry(engine, BTC, "buy", "REJECTED")  # journalled REJECTED after a timeout
    ex = FakeExchange()
    _protected(ex, BTC)  # ...but OKX accepted it
    bot = RecordingBot()
    _tick(ex, engine, tmp_path, bot)
    assert _orphan_alerts(bot)


def test_a_position_from_an_accepted_entry_is_not_an_orphan(engine: Engine, tmp_path: Path) -> None:
    _entry(engine, BTC, "buy", "OPEN", "FILLED")
    ex = FakeExchange()
    _protected(ex, BTC)
    bot = RecordingBot()
    result = _tick(ex, engine, tmp_path, bot)
    assert not _orphan_alerts(bot)
    assert result.discrepancies == []


def test_a_filled_entry_with_nothing_at_the_venue_is_reported_as_a_ghost(
    engine: Engine, tmp_path: Path
) -> None:
    _entry(engine, ETH, "buy", "OPEN", "FILLED")
    bot = RecordingBot()
    result = _tick(FakeExchange(), engine, tmp_path, bot)
    assert any("ghost" in d for d in result.discrepancies)
    assert not _orphan_alerts(bot), "a ghost is a bookkeeping warning, not a page"


def test_the_orphan_is_journalled_as_a_position_event(engine: Engine, tmp_path: Path) -> None:
    ex = FakeExchange()
    _protected(ex, BTC)
    _tick(ex, engine, tmp_path, RecordingBot())
    with engine.connect() as conn:
        states = conn.execute(
            text("SELECT to_state FROM position_events WHERE machine = 'position'")
        ).scalars()
        assert "ORPHAN" in list(states)
