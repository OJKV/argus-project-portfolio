"""Journal migrations, WAL, run context, and the ledger hash chain (MEMORY_AND_STATE.md §4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.journal import ledger
from argus.journal.db import make_engine, migrate, open_journal, start_run


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    eng = make_engine(tmp_path / "j.db")
    first = migrate(eng)
    second = migrate(eng)
    # Numbered order, not a pinned filename: naming the last migration means every new one
    # breaks this test for no reason (the same brittleness that hid the dead LLM catalogue).
    assert first[0] == "001_core.sql"
    assert first == sorted(first)
    assert all(name[:3].isdigit() and name.endswith(".sql") for name in first)
    assert second == []


def test_wal_and_foreign_keys_are_on(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_start_run_closes_ok(engine: Engine) -> None:
    with start_run(engine, "watchdog", config_hash="abc") as run_id:
        pass
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, finished_at, mode FROM runs WHERE run_id=:r"), {"r": run_id}
        ).one()
    assert row.status == "OK"
    assert row.finished_at is not None
    assert row.mode == "demo"


def test_start_run_records_failure_and_reraises(engine: Engine) -> None:
    with pytest.raises(RuntimeError, match="boom"), start_run(engine, "manager", "abc") as run_id:
        captured = run_id
        raise RuntimeError("boom")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, error FROM runs WHERE run_id=:r"), {"r": captured}
        ).one()
    assert row.status == "FAILED"
    assert "boom" in row.error


def _run(engine: Engine) -> str:
    with start_run(engine, "manager", "cfg") as run_id:
        return run_id


def test_ledger_chain_links_rows_and_verifies(engine: Engine) -> None:
    run_id = _run(engine)
    ids = [
        ledger.append(
            engine,
            "position_events",
            {
                "run_id": run_id,
                "symbol": "BTC-USDT-SWAP",
                "machine": "position",
                "from_state": None,
                "to_state": "OPEN",
                "reason": f"event {n}",
            },
        )
        for n in range(3)
    ]
    assert len(set(ids)) == 3
    assert ledger.verify(engine, "position_events") == (True, None)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT prev_hash, row_hash FROM position_events ORDER BY rowid")
        ).fetchall()
    assert rows[0].prev_hash == ledger.GENESIS
    assert rows[1].prev_hash == rows[0].row_hash
    assert rows[2].prev_hash == rows[1].row_hash


def test_ledger_detects_tampering(engine: Engine) -> None:
    run_id = _run(engine)
    ledger.append(
        engine,
        "position_events",
        {
            "run_id": run_id,
            "symbol": "ETH",
            "machine": "position",
            "to_state": "OPEN",
            "reason": "a",
        },
    )
    target = ledger.append(
        engine,
        "position_events",
        {
            "run_id": run_id,
            "symbol": "ETH",
            "machine": "position",
            "to_state": "CLOSED",
            "reason": "b",
        },
    )
    ledger.append(
        engine,
        "position_events",
        {
            "run_id": run_id,
            "symbol": "ETH",
            "machine": "protection",
            "to_state": "EXITED",
            "reason": "c",
        },
    )
    with engine.begin() as conn:  # someone drops the guard and edits history in place
        conn.exec_driver_sql("DROP TRIGGER position_events_no_update")
        conn.execute(
            text("UPDATE position_events SET reason='edited' WHERE event_id=:i"), {"i": target}
        )
    ok, broken = ledger.verify(engine, "position_events")
    assert not ok
    assert broken == target


def test_verify_all_covers_every_ledger_table(engine: Engine) -> None:
    results = ledger.verify_all(engine)
    assert set(results) == set(ledger.LEDGER_TABLES)
    assert all(ok for ok, _ in results.values())


def test_non_ledger_table_is_refused(engine: Engine) -> None:
    with pytest.raises(ValueError, match="not a ledger table"):
        ledger.append(engine, "runs", {})
