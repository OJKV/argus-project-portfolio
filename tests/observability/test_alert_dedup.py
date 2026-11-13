"""An alert that stays true is repeated on a cooldown, not on every tick (review O-3).

F-141: `manager_stale` fired 364 times — every 20 minutes for 6.7 days. A channel that says the
same thing 72 times a day gets muted, and the next P1 lands in the muted channel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.observability import alerts

T0 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _ticks(hours: float, every_min: int = 20) -> list[datetime]:
    return [T0 + timedelta(minutes=m) for m in range(0, int(hours * 60), every_min)]


def test_a_stale_manager_is_reported_four_times_a_day_not_seventy_two(engine: Engine) -> None:
    bot = RecordingBot()
    notifier = Notifier(bot=bot, chat_id="42")
    last_run = T0 - timedelta(hours=20)
    for now in _ticks(24):
        alerts.dispatch(
            [alerts.stale_manager(last_run, now, 14.0)], notifier=notifier, engine=engine, now=now
        )
    assert len(bot.sent) == 24 // 6  # the message changes each tick (the gap grows); the key not


def test_a_critical_alert_repeats_hourly(engine: Engine) -> None:
    bot = RecordingBot()
    notifier = Notifier(bot=bot, chat_id="42")
    for now in _ticks(3):
        alerts.dispatch(
            [alerts.unprotected_positions(1)], notifier=notifier, engine=engine, now=now
        )
    assert len(bot.sent) == 3


def test_a_new_key_is_not_held_back_by_another_keys_cooldown(engine: Engine) -> None:
    bot = RecordingBot()
    notifier = Notifier(bot=bot, chat_id="42")
    alerts.dispatch(
        [alerts.stale_manager(None, T0, 14.0)], notifier=notifier, engine=engine, now=T0
    )
    later = T0 + timedelta(minutes=20)
    alerts.dispatch(
        [alerts.stale_manager(None, later, 14.0), alerts.unprotected_positions(2)],
        notifier=notifier,
        engine=engine,
        now=later,
    )
    assert len(bot.sent) == 2
    assert "positions_unprotected" in bot.sent[1][1]
    assert "manager_stale" not in bot.sent[1][1]  # still cooling down, so not repeated with it


def test_an_undelivered_alert_is_retried_on_the_next_tick(engine: Engine) -> None:
    """Only a delivery starts the cooldown — a dead channel must not swallow the first one."""

    class DownThenUp:
        def __init__(self) -> None:
            self.calls = 0
            self.delivered: list[str] = []

        def send(self, text: str) -> bool:
            self.calls += 1
            if self.calls == 1:
                return False
            self.delivered.append(text)
            return True

    channel = DownThenUp()
    for now in _ticks(1):
        alerts.dispatch(
            [alerts.stale_manager(None, now, 14.0)], notifier=channel, engine=engine, now=now
        )
    assert channel.calls == 2
    assert len(channel.delivered) == 1


def test_the_watchdog_tick_uses_the_cooldown(engine: Engine, tmp_path: Path) -> None:
    """The wiring: the 364 came from the watchdog, so the watchdog must pass the journal."""
    from argus.runs.watchdog import Watchdog

    bot = RecordingBot()
    watchdog = Watchdog(
        exchange=FakeExchange(),
        engine=engine,
        notifier=Notifier(bot=bot, chat_id="42"),
        settings=Settings(mode="demo", argus_home=tmp_path),
    )
    for _ in range(3):
        with start_run(engine, "watchdog", "cfg") as run_id:
            watchdog.run_once(run_id)
    stale = [text for _, text in bot.sent if "manager_stale" in text]
    assert len(stale) == 1
