"""antigravity design note: 'database is locked' under concurrent UI + watchdog access.

Already mitigated by WAL + `busy_timeout=5000` + runs serialised by a lock file. This is a
regression guard so the mitigation cannot be removed silently — not a bug fix.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from argus.journal.db import open_journal


def test_writers_wait_for_a_lock_instead_of_failing_immediately(tmp_path: Path) -> None:
    engine = open_journal(tmp_path / "j.db")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000


def test_a_reader_is_not_blocked_by_an_open_write_transaction(tmp_path: Path) -> None:
    path = tmp_path / "j.db"
    writer, reader = open_journal(path), open_journal(path)
    with writer.begin() as wconn:
        wconn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash) "
                "VALUES ('r1', 'watchdog', 'demo', '2026-09-17T00:00:00+00:00', 'RUNNING', 'x')"
            )
        )
        with reader.connect() as rconn:  # WAL: readers see the last commit, never block
            assert rconn.execute(text("SELECT COUNT(*) FROM runs")).scalar() == 0
    with reader.connect() as rconn:
        assert rconn.execute(text("SELECT COUNT(*) FROM runs")).scalar() == 1
