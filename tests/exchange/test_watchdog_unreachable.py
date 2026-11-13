"""Review 2026-09-18 X-10: the `exchange_unreachable` alert could never fire in production.

Every watchdog tick is a fresh process with a fresh in-memory `BreakerRegistry`, so
`attempts >= 3 and state == "open"` was never true. The old test passed only because it reused
one Watchdog across three runs. The count has to live where the next process can read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import UNREACHABLE_ALERT_AFTER, Watchdog


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _tick(engine: Engine, tmp_path: Path, bot: RecordingBot, down: bool) -> None:
    """One systemd tick: a new process, a new adapter, a new Watchdog, a new breaker registry."""
    ex = FakeExchange()
    if down:
        ex.fail_next = TimeoutError("venue down")
    with start_run(engine, "watchdog", "demo") as run_id:
        Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=bot, chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
        ).run_once(run_id)


def _unreachable_alerts(bot: RecordingBot) -> int:
    return sum("Exchange unreachable" in text for _, text in bot.sent)


def test_three_failed_ticks_in_three_processes_raise_the_alert(
    engine: Engine, tmp_path: Path
) -> None:
    bot = RecordingBot()
    for _ in range(UNREACHABLE_ALERT_AFTER - 1):
        _tick(engine, tmp_path, bot, down=True)
    assert _unreachable_alerts(bot) == 0, "one or two blips are routine venue weather (F-140)"
    _tick(engine, tmp_path, bot, down=True)
    assert _unreachable_alerts(bot) == 1


def test_a_successful_tick_resets_the_count(engine: Engine, tmp_path: Path) -> None:
    bot = RecordingBot()
    _tick(engine, tmp_path, bot, down=True)
    _tick(engine, tmp_path, bot, down=True)
    _tick(engine, tmp_path, bot, down=False)
    _tick(engine, tmp_path, bot, down=True)
    assert _unreachable_alerts(bot) == 0
