"""A watchdog tick that could not take the lock is journalled, and repeated skips alert (J-4).

On LockBusy the watchdog printed a line and exited 0: no run row, no alert, and the unit pinged
healthchecks "OK". A stop cancelled while a long manager run held the lock could sit unverified
for several ticks with every monitor green.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from argus.journal import db
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot


@pytest.fixture
def engine() -> Engine:
    return open_journal()


@pytest.fixture
def busy(monkeypatch: pytest.MonkeyPatch) -> RecordingBot:
    """The lock is held by someone else; alerts go to a recorder."""
    import argus.cli as cli
    import argus.runs.lock as lock

    @contextmanager
    def held(kind: str, home: Path, timeout_s: float = 0.0) -> Iterator[bool]:
        raise lock.LockBusyError("manager holds the run lock")
        yield True  # pragma: no cover

    bot = RecordingBot()
    monkeypatch.setattr(lock, "run_lock", held)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(Notifier, "from_settings", classmethod(lambda cls, s=None: cls(bot, "42")))
    return bot


def _statuses(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status FROM runs WHERE kind = 'watchdog' ORDER BY rowid")
        ).all()
    return [str(r[0]) for r in rows]


def test_one_skipped_tick_is_journalled_but_not_alarming(
    engine: Engine, busy: RecordingBot
) -> None:
    from argus.cli import app

    result = CliRunner().invoke(app, ["watchdog", "--once"])

    assert result.exit_code == 0, result.output
    assert _statuses(engine) == ["SKIPPED"]
    assert busy.sent == []  # the next tick is 20 minutes away


def test_a_second_consecutive_skip_alerts_and_exits_non_zero(
    engine: Engine, busy: RecordingBot
) -> None:
    from argus.cli import SKIPPED_EXIT, app

    CliRunner().invoke(app, ["watchdog", "--once"])
    result = CliRunner().invoke(app, ["watchdog", "--once"])

    assert result.exit_code == SKIPPED_EXIT != 0
    assert _statuses(engine) == ["SKIPPED", "SKIPPED"]
    assert any("watchdog_skipped" in t for _, t in busy.sent), busy.sent


def test_a_completed_tick_resets_the_count(engine: Engine) -> None:
    db.record_skipped_run(engine, "watchdog", "busy")
    with start_run(engine, "watchdog", config_hash="-"):
        pass
    db.record_skipped_run(engine, "watchdog", "busy")
    assert db.consecutive_skips(engine, "watchdog") == 1


def test_skips_count_by_time_not_by_insertion(engine: Engine, monkeypatch: Any) -> None:
    db.record_skipped_run(engine, "watchdog", "busy")
    db.record_skipped_run(engine, "watchdog", "busy")
    assert db.consecutive_skips(engine, "watchdog") == 2
    later = datetime.now(UTC) + timedelta(minutes=20)
    monkeypatch.setattr(db, "utc_now", lambda: later.replace(microsecond=0).isoformat())
    with start_run(engine, "watchdog", config_hash="-"):
        pass
    assert db.consecutive_skips(engine, "watchdog") == 0
