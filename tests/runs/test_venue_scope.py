"""One journal, two accounts: the watchdog's reads must stay on its own (review of PR #1, F-149).

A journal holds rows from the OKX demo account and the live account; a run records which one in
`runs.config_hash`. Three money-path reads ignored it:

* the kill switch's 30-day peak — demo at $10 000, then live at $200 and $198, read as a 98%
  drawdown, wrote HALT and flattened the live book every 20 minutes;
* the position snapshots — a live tick "closed" a demo position, feeding fake post-mortems to the
  Reflector, and a newer demo row could hide a live one from the unreachable-venue check;
* the repair stop — a later demo entry on the same asset supplied the stop for a live position,
  which can widen it.

Every account here is a `FakeExchange`; the live `Settings` only names the account.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.config import LIVE_VENUE_WORD, Settings
from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import record_order, transition_order
from argus.journal import ledger
from argus.journal import positions as positions_journal
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import (
    Watchdog,
    WatchdogResult,
    equity_peak_30d,
    journal_positions,
    plan_stops_from_journal,
    read_halt,
    record_equity,
)

BTC = "BTC-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _settings(venue: str, tmp_path: Path) -> Settings:
    if venue == "demo":
        return Settings(_env_file=None, mode="demo", argus_home=tmp_path)  # type: ignore[call-arg]
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        mode="auto",
        live_trading="I_UNDERSTAND",
        okx_demo=False,
        live_venue=LIVE_VENUE_WORD,
        argus_home=tmp_path,
    )


def _tick(engine: Engine, tmp_path: Path, venue: str, ex: FakeExchange) -> WatchdogResult:
    settings = _settings(venue, tmp_path)
    assert settings.venue == venue
    with start_run(engine, "watchdog", config_hash=settings.venue) as run_id:
        return Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=RecordingBot(), chat_id="42"),
            settings=settings,
        ).run_once(run_id)


def _equity(engine: Engine, venue: str, equity: str) -> None:
    with start_run(engine, "watchdog", config_hash=venue) as run_id:
        record_equity(engine, run_id, Decimal(equity))


def _open(engine: Engine, venue: str, symbol: str = BTC) -> str:
    with start_run(engine, "watchdog", config_hash=venue) as run_id:
        positions_journal.record(
            engine,
            run_id,
            [{"symbol": symbol, "side": "long", "size": "1", "entry_price": 1, "mark_price": 1}],
        )
    return run_id


def _entry(engine: Engine, venue: str, stop: str) -> None:
    """A long BTC entry the venue accepted, its stop attached — as a manager run journals it."""
    with start_run(engine, "manager", config_hash=venue) as run_id:
        plan_id = ledger.append(
            engine,
            "plans",
            {
                "run_id": run_id,
                "mode": "approve",
                "symbol": "BTC",
                "side": "long",
                "state": "GATED",
                "payload": json.dumps({"stop": float(stop), "side": "long"}),
            },
        )
        ledger.append(
            engine,
            "gate_results",
            {"plan_id": plan_id, "run_id": run_id, "verdict": "APPROVED", "reasons": "[]"},
        )
        order = {
            "symbol": BTC,
            "side": "buy",
            "size": Decimal("1"),
            "order_type": "limit",
            "price": Decimal("100000"),
            "cl_ord_id": f"cl{plan_id[:20]}",
            "attach_algos": [{"kind": "sl", "trigger_px": Decimal(stop)}],
        }
        transition_order(
            engine, record_order(engine, run_id, order, plan_id=plan_id), "OPEN", "accepted"
        )


def _closed_rows(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM positions_snapshots WHERE state = 'CLOSED'")
            ).scalar()
            or 0
        )


# --- 1. the kill switch's peak --------------------------------------------------------------


def test_a_demo_peak_does_not_flatten_the_live_account(engine: Engine, tmp_path: Path) -> None:
    _equity(engine, "demo", "10000")
    _equity(engine, "live", "200")
    ex = FakeExchange(equity=Decimal("198"))
    ex.open_unprotected(BTC)
    ex.amend_stop(BTC, Decimal("70000"))

    result = _tick(engine, tmp_path, "live", ex)

    assert not result.killed
    assert read_halt(_settings("live", tmp_path).halt_file) is None
    assert ex.fetch_positions(), "a 1% drawdown closes nothing"
    assert result.drawdown_pct == Decimal("1")


def test_the_peak_is_read_from_the_named_account_only(engine: Engine) -> None:
    _equity(engine, "demo", "10000")
    _equity(engine, "live", "200")
    _equity(engine, "-", "50000")  # a pre-F-131 run belongs to neither account

    assert equity_peak_30d(engine, Decimal("198"), venue="live") == Decimal("200")
    assert equity_peak_30d(engine, Decimal("9000"), venue="demo") == Decimal("10000")


# --- 2. the position snapshots --------------------------------------------------------------


def test_a_live_tick_does_not_close_a_demo_position(engine: Engine, tmp_path: Path) -> None:
    _open(engine, "demo")

    result = _tick(engine, tmp_path, "live", FakeExchange())  # the live book is empty

    assert _closed_rows(engine) == 0, "the live account never held it, so it never closed"
    assert not any("closed since last tick" in n for n in result.notes)
    assert set(positions_journal.latest_open(engine, venue="demo")) == {BTC}
    assert positions_journal.newly_closed(engine, [], venue="live") == []


def test_a_newer_demo_close_does_not_hide_an_open_live_position(engine: Engine) -> None:
    _open(engine, "live")
    demo_run = _open(engine, "demo")
    positions_journal.mark_closed(
        engine, demo_run, positions_journal.newly_closed(engine, [], venue="demo")
    )

    assert set(positions_journal.latest_open(engine, venue="live")) == {BTC}
    assert [c.symbol for c in positions_journal.newly_closed(engine, [], venue="live")] == [BTC]
    # An unreachable live venue with a live position on the book is an incident, not weather.
    assert not WatchdogResult(run_id="r", unreachable=True).degraded_not_failed(
        engine, venue="live"
    )


def test_a_demo_position_does_not_make_a_live_outage_an_incident(engine: Engine) -> None:
    _open(engine, "demo")
    assert WatchdogResult(run_id="r", unreachable=True).degraded_not_failed(engine, venue="live")


# --- 3. the repair stop ---------------------------------------------------------------------


def test_a_later_demo_entry_never_supplies_the_live_stop(engine: Engine) -> None:
    _entry(engine, "live", stop="95000")
    _entry(engine, "demo", stop="90000")  # later, and wider for a long

    assert plan_stops_from_journal(engine, [], venue="live") == {("btc", "long"): Decimal("95000")}
    assert plan_stops_from_journal(engine, [], venue="demo") == {("btc", "long"): Decimal("90000")}
    live_book = journal_positions(engine, [], venue="live")
    assert [(p["side"], p["size"]) for p in live_book] == [("long", Decimal("1"))]


def test_a_demo_close_does_not_end_a_live_trade(engine: Engine) -> None:
    """A demo CLOSED row newer than a live entry on the same asset must not retire that entry.

    Read across accounts, the live position lost its plan stop (repaired from the ATR instead)
    and was paged as an orphan, because its entry looked like part of a trade that had closed.
    """
    _entry(engine, "live", stop="95000")
    demo_run = _open(engine, "demo")
    positions_journal.mark_closed(
        engine,
        demo_run,
        positions_journal.newly_closed(engine, [], venue="demo"),
        now=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert plan_stops_from_journal(engine, [], venue="live") == {("btc", "long"): Decimal("95000")}
    assert [p["size"] for p in journal_positions(engine, [], venue="live")] == [Decimal("1")]


def test_the_live_watchdog_repairs_with_the_live_entrys_stop(
    engine: Engine, tmp_path: Path
) -> None:
    _entry(engine, "live", stop="95000")
    _entry(engine, "demo", stop="90000")
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    ex.open_unprotected(BTC, side="long")  # the live stop has vanished

    result = _tick(engine, tmp_path, "live", ex)

    assert result.repaired == [BTC]
    stop = next(a for a in ex.algos.values() if a.kind == "sl")
    assert stop.trigger_px == Decimal("95000")
