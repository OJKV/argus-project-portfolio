"""Review 2026-10-07 (exec): the two ways the kill-switch path could leave entries un-halted.

* X-3 made the flatten wait for a second balance read. When that read *failed*, the check
  returned with an error and no HALT: the manager kept opening positions into a drawdown the
  watchdog had seen and could not confirm — and the watchdog's HALT is the only thing that stops
  it (`gate/killswitch.portfolio_kill` has no production caller).
* In the confirmed path, `write_halt` was the one unguarded step. A full disk or an unwritable
  halt dir raised out of `_drawdown_check`, so `_flatten` and the alert never ran, tick after tick.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs import watchdog as watchdog_module
from argus.runs.watchdog import KILL_MARKER, Watchdog, WatchdogResult, read_halt

BTC = "BTC-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(mode="demo", argus_home=tmp_path)


def _tick(wd: Watchdog, engine: Engine) -> WatchdogResult:
    with start_run(engine, "watchdog", "demo") as run_id:
        return wd.run_once(run_id)


def _armed(engine: Engine, settings: Settings, bot: RecordingBot) -> tuple[FakeExchange, Watchdog]:
    ex = FakeExchange(equity=Decimal("1000"))
    ex.open_unprotected(BTC)
    ex.amend_stop(BTC, Decimal("70000"))
    wd = Watchdog(
        exchange=ex, engine=engine, notifier=Notifier(bot=bot, chat_id="42"), settings=settings
    )
    _tick(wd, engine)  # the 30-day peak, 1000
    return ex, wd


def _balances(ex: FakeExchange, readings: list[Decimal | Exception]) -> None:
    queue: Iterator[Decimal | Exception] = iter(readings)

    def scripted() -> dict[str, Any]:
        reading = next(queue)
        if isinstance(reading, Exception):
            raise reading
        return {**FakeExchange.fetch_balance(ex), "equity": reading}

    ex.fetch_balance = scripted  # type: ignore[method-assign]


def test_an_unconfirmed_breach_halts_new_entries_and_closes_nothing(
    engine: Engine, settings: Settings
) -> None:
    bot = RecordingBot()
    ex, wd = _armed(engine, settings, bot)
    _balances(ex, [Decimal("800"), RuntimeError("venue maintenance")])

    result = _tick(wd, engine)

    halt = read_halt(settings.halt_file)
    assert halt is not None, "the manager must stop opening positions"
    assert KILL_MARKER not in halt, "an unconfirmed breach must not arm the halted flatten retry"
    assert ex.fetch_positions(), "nothing may be closed on one unconfirmed reading"
    assert not result.killed and not result.ok
    assert any("HALT" in text for _, text in bot.sent)


def test_a_breach_confirmed_on_a_later_tick_still_flattens(
    engine: Engine, settings: Settings
) -> None:
    ex, wd = _armed(engine, settings, RecordingBot())
    _balances(ex, [Decimal("800"), RuntimeError("venue maintenance")])
    _tick(wd, engine)

    _balances(ex, [Decimal("800"), Decimal("800")])
    result = _tick(wd, engine)

    assert result.killed
    assert ex.fetch_positions() == []
    assert KILL_MARKER in (read_halt(settings.halt_file) or "")


def test_a_provisional_halt_is_left_for_the_operator_when_the_reads_recover(
    engine: Engine, settings: Settings
) -> None:
    ex, wd = _armed(engine, settings, RecordingBot())
    _balances(ex, [Decimal("800"), RuntimeError("venue maintenance")])
    _tick(wd, engine)

    _balances(ex, [Decimal("990")])
    result = _tick(wd, engine)

    assert not result.killed
    assert ex.fetch_positions()
    assert read_halt(settings.halt_file) is not None
    assert any("clear" in note.lower() for note in result.notes)


def test_a_halt_that_cannot_be_written_does_not_stop_the_flatten(
    engine: Engine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = RecordingBot()
    ex, wd = _armed(engine, settings, bot)
    ex.equity = Decimal("800")

    def disk_full(path: Path, reason: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(watchdog_module, "write_halt", disk_full)
    result = _tick(wd, engine)

    assert result.killed
    assert ex.fetch_positions() == [], "the flatten must run although HALT could not be written"
    assert not result.ok
    assert any("HALT" in error for error in result.errors)
    assert any("drawdown" in text.lower() for _, text in bot.sent)
