"""A migration applies completely or not at all (review J-5 / J-N6).

pysqlite's legacy transaction mode autocommits DDL that runs before the first INSERT, so a
migration that failed half-way left its first tables behind and every later `open_journal()` —
the watchdog's included — died on "table already exists" until someone repaired it by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from argus.journal import db


def _tables(engine: object) -> set[str]:
    return set(inspect(engine).get_table_names())  # type: ignore[arg-type]


def test_a_failing_migration_leaves_no_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_half.sql").write_text(
        "CREATE TABLE first_half (x INTEGER);\nCREATE TABLE broken (;\n", encoding="utf-8"
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    engine = db.make_engine(tmp_path / "j.db")

    with pytest.raises(OperationalError):
        db.migrate(engine)

    assert "first_half" not in _tables(engine)
    with engine.connect() as conn:
        assert db.applied_migrations(conn) == set()

    # Fixing the file is then enough: the retry applies cleanly instead of colliding with debris.
    (migrations / "001_half.sql").write_text(
        "CREATE TABLE first_half (x INTEGER);\nCREATE TABLE second_half (y INTEGER);\n",
        encoding="utf-8",
    )
    assert db.migrate(engine) == ["001_half.sql"]
    assert {"first_half", "second_half"} <= _tables(engine)


def test_a_trigger_body_is_not_split_at_its_inner_semicolons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`split(";\\n")` cut a BEGIN … END body in half; ledger guards are triggers (J-3)."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_trigger.sql").write_text(
        "-- a comment with a ; in it;\n"
        "CREATE TABLE t (x INTEGER);\n"
        "CREATE TRIGGER t_no_delete BEFORE DELETE ON t\n"
        "BEGIN\n"
        "    SELECT RAISE(ABORT, 't is append-only');\n"
        "END;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    engine = db.make_engine(tmp_path / "j.db")

    assert db.migrate(engine) == ["001_trigger.sql"]
    with engine.connect() as conn:
        names = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master")}
    assert "t_no_delete" in names


def test_busy_timeout_is_set_before_the_journal_mode_switch(tmp_path: Path) -> None:
    """WAL needs a lock to switch; without the timeout first, a busy file fails immediately."""
    engine = db.make_engine(tmp_path / "j.db")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    import inspect as pyinspect

    source = pyinspect.getsource(db._sqlite_pragmas)
    assert source.index("busy_timeout") < source.index("journal_mode")


def test_opening_a_current_journal_does_not_wait_on_the_write_lock(tmp_path: Path) -> None:
    """With nothing pending, opening the journal is a read. The watchdog opens it before it
    verifies any stop, so a writer holding the lock past busy_timeout must not kill the tick."""
    import sqlite3

    path = tmp_path / "j.db"
    db.open_journal(path).dispose()
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        engine = db.open_journal(path)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM schema_migrations").scalar()
    finally:
        holder.rollback()
        holder.close()


def test_a_pending_migration_still_applies_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_a.sql").write_text("CREATE TABLE a (x INTEGER);\n", encoding="utf-8")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    engine = db.make_engine(tmp_path / "j.db")
    assert db.migrate(engine) == ["001_a.sql"]
    assert db.migrate(engine) == []

    (migrations / "002_b.sql").write_text("CREATE TABLE b (y INTEGER);\n", encoding="utf-8")
    assert db.migrate(engine) == ["002_b.sql"]
    assert {"a", "b"} <= _tables(engine)
