"""An applied migration is history, not source (F-085, 2026-09-20).

`llm_attempts` was appended to `005_llm.sql` after 005 had already run on the VPS. Migrations are
tracked by filename, so the new statement never executed there and the first live LLM run died
with `no such table` on every asset — while every laptop, whose journal was created from scratch,
was perfectly happy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import text

from argus.journal.db import MIGRATIONS_DIR, make_engine, migrate


def test_editing_an_applied_migration_is_detected(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "j.db")
    migrate(engine)

    target = sorted(MIGRATIONS_DIR.glob("*.sql"))[0]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schema_migrations SET checksum = 'tampered' WHERE name = :n"),
            {"n": target.name},
        )

    # A changed file cannot be re-applied — the fix is always a NEW migration — so the runner has
    # to say so rather than silently leave two machines with different schemas.
    try:
        migrate(engine)
    except RuntimeError as exc:
        assert target.name in str(exc)
    else:
        raise AssertionError("a modified migration must not pass unnoticed")


def test_checksums_are_recorded_for_every_applied_migration(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "j.db")
    migrate(engine)
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT name, checksum FROM schema_migrations")).all())
    for path in MIGRATIONS_DIR.glob("*.sql"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert rows[path.name] == digest, path.name


def test_llm_attempts_exists_on_a_journal_built_from_scratch(tmp_path: Path) -> None:
    from argus.journal.db import open_journal

    engine = open_journal(tmp_path / "j.db")
    with engine.connect() as conn:
        tables = {
            r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert {"llm_calls", "llm_quota", "llm_attempts"} <= tables


def test_reconciling_re_records_checksums_and_says_what_it_blessed(tmp_path: Path) -> None:
    """The guard is strict by design, so there has to be a deliberate way through it.

    Editing an applied migration is a mistake, but pretending it did not happen is worse: after a
    knowing reconciliation (say, splitting one file into two), the recorded history has to be
    brought back in line — explicitly, and saying out loud which files it re-blessed.
    """
    from argus.journal.db import reconcile_migrations

    engine = make_engine(tmp_path / "j.db")
    migrate(engine)
    target = sorted(MIGRATIONS_DIR.glob("*.sql"))[0]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schema_migrations SET checksum = 'stale' WHERE name = :n"),
            {"n": target.name},
        )

    blessed = reconcile_migrations(engine)
    assert blessed == [target.name]
    migrate(engine)  # no longer raises

    assert reconcile_migrations(engine) == [], "a second run has nothing left to reconcile"
