"""Ledger tables are insert-only, and the chain still verifies after a placement (J-N1 / J-3).

`record_venue_ids` UPDATEs `orders` once the venue answers. Before this the hash covered those
columns, so every placed order broke the `orders` chain the moment its ids were stored — and
nothing noticed, because nothing ever called `verify_all`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from argus.execution.lifecycle import record_order, record_venue_ids
from argus.journal import ledger
from argus.journal.db import open_journal, start_run

ORDER = {
    "cl_ord_id": "argus-abc",
    "symbol": "BTC-USDT-SWAP",
    "side": "buy",
    "order_type": "limit",
    "price": "70000",
    "size": "1",
}


@pytest.fixture
def engine() -> Engine:
    # The default path, so `argus audit` reads the same journal (conftest isolates ARGUS_HOME).
    return open_journal()


def _run(engine: Engine) -> str:
    with start_run(engine, "test", config_hash="-") as run_id:
        return run_id


def _one_row_in_every_ledger_table(engine: Engine) -> str:
    run_id = _run(engine)
    plan_id = ledger.append(
        engine,
        "plans",
        {"run_id": run_id, "mode": "demo", "symbol": "BTC", "state": "GATED", "payload": "{}"},
    )
    ledger.append(
        engine,
        "gate_results",
        {"plan_id": plan_id, "run_id": run_id, "verdict": "APPROVED", "reasons": "[]"},
    )
    order_id = record_order(engine, run_id, ORDER, plan_id=plan_id)
    ledger.append(
        engine,
        "fills",
        {
            "order_id": order_id,
            "run_id": run_id,
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "price": "70000",
            "size": "1",
            "fee": "-0.1",
            "fee_ccy": "USDT",
            "raw": "{}",
        },
    )
    ledger.append(
        engine,
        "position_events",
        {"run_id": run_id, "symbol": "BTC", "machine": "position", "to_state": "OPEN"}
        | {"reason": "x"},
    )
    ledger.append(
        engine,
        "kill_switches",
        {"run_id": run_id, "scope": "portfolio", "rule": "dd", "triggered": 0, "detail": "x"},
    )
    return order_id


def test_storing_venue_ids_keeps_the_orders_chain_verifiable(engine: Engine) -> None:
    order_id = _one_row_in_every_ledger_table(engine)
    assert ledger.verify(engine, "orders") == (True, None)

    record_venue_ids(engine, order_id, {"entry_id": "2981234567890", "sl_id": "778899"})

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT exchange_id, algo_id FROM orders WHERE order_id = :o"), {"o": order_id}
        ).one()
    assert tuple(row) == ("2981234567890", "778899")  # readers still find them on the row
    assert all(ok for ok, _ in ledger.verify_all(engine).values()), ledger.verify_all(engine)


def test_venue_ids_are_set_once_and_never_rewritten(engine: Engine) -> None:
    order_id = _one_row_in_every_ledger_table(engine)
    record_venue_ids(engine, order_id, {"entry_id": "111"})
    record_venue_ids(engine, order_id, {"entry_id": "111"})  # an idempotent repeat is harmless

    with pytest.raises(IntegrityError, match="insert-only"), engine.begin() as conn:
        conn.execute(
            text("UPDATE orders SET exchange_id = '222' WHERE order_id = :o"), {"o": order_id}
        )


def test_every_other_column_of_every_ledger_table_refuses_an_update(engine: Engine) -> None:
    """Read from the live schema, so a column a future migration adds is covered too."""
    _one_row_in_every_ledger_table(engine)
    for table in ledger.LEDGER_TABLES:
        with engine.connect() as conn:
            columns = [r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")]
        for column in columns:
            if column in ledger.WRITE_ONCE.get(table, frozenset()):
                continue
            with pytest.raises(IntegrityError, match="insert-only"), engine.begin() as conn:
                conn.exec_driver_sql(f"UPDATE {table} SET {column} = 'tampered'")


def test_no_ledger_row_can_be_deleted(engine: Engine) -> None:
    """An emptied table verified as (True, None): truncation was invisible to `verify` (J-3)."""
    _one_row_in_every_ledger_table(engine)
    for table in ledger.LEDGER_TABLES:
        with pytest.raises(IntegrityError, match="insert-only"), engine.begin() as conn:
            conn.exec_driver_sql(f"DELETE FROM {table}")


def test_argus_audit_passes_a_clean_journal_and_fails_a_tampered_one(engine: Engine) -> None:
    from argus.cli import app

    order_id = _one_row_in_every_ledger_table(engine)
    record_venue_ids(engine, order_id, {"entry_id": "2981234567890"})
    clean = CliRunner().invoke(app, ["audit"])
    assert clean.exit_code == 0, clean.output
    assert "orders" in clean.output

    # Someone with shell access drops the guard and edits history in place.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER orders_write_once")
        conn.execute(text("UPDATE orders SET size = '100' WHERE order_id = :o"), {"o": order_id})
    tampered = CliRunner().invoke(app, ["audit"])
    assert tampered.exit_code == 1
    assert order_id[:8] in tampered.output


def test_audit_reports_a_missing_guard_even_when_every_chain_verifies(engine: Engine) -> None:
    _one_row_in_every_ledger_table(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER fills_no_delete")
    report = {r.table: r for r in ledger.audit(engine)}
    assert report["fills"].chain_ok
    assert report["fills"].missing_guards == ["fills_no_delete"]
    assert not report["fills"].ok
    assert report["orders"].ok


def test_audit_reports_a_guard_recreated_as_a_no_op(engine: Engine) -> None:
    """Drop the guard, delete the tail, recreate a trigger under the same name that does nothing.
    Every chain verifies and every name is present; only the trigger's text gives it away."""
    from argus.cli import app

    _one_row_in_every_ledger_table(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER fills_no_delete")
        conn.exec_driver_sql("DELETE FROM fills")
        conn.exec_driver_sql(
            "CREATE TRIGGER fills_no_delete BEFORE DELETE ON fills BEGIN SELECT 1; END"
        )
    report = {r.table: r for r in ledger.audit(engine)}
    assert report["fills"].chain_ok and report["fills"].missing_guards == []
    assert report["fills"].altered_guards == ["fills_no_delete"]
    assert not report["fills"].ok
    assert report["orders"].ok and report["orders"].altered_guards == []
    tampered = CliRunner().invoke(app, ["audit"])
    assert tampered.exit_code == 1
    assert "altered fills_no_delete" in tampered.output


def test_audit_rows_and_head_let_an_operator_anchor_the_chain(engine: Engine) -> None:
    _one_row_in_every_ledger_table(engine)
    report = {r.table: r for r in ledger.audit(engine)}
    assert report["orders"].rows == 1
    assert report["orders"].head == ledger.last_hash_of(engine, "orders")
    assert len(report["orders"].head) == 64


def test_a_journal_written_before_the_guards_still_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The VPS journal predates the triggers and holds orders whose ids were UPDATEd in."""
    import shutil

    from argus.journal import db

    before = tmp_path / "migrations_before_guards"
    before.mkdir()
    for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "010":
            shutil.copy(path, before / path.name)
    journal = tmp_path / "old.db"
    monkeypatch.setattr(db, "MIGRATIONS_DIR", before)
    old = open_journal(journal)
    order_id = _one_row_in_every_ledger_table(old)
    record_venue_ids(old, order_id, {"entry_id": "1", "sl_id": "2"})

    monkeypatch.setattr(db, "MIGRATIONS_DIR", db.Path(db.__file__).parent / "migrations")
    report = ledger.audit(open_journal(journal))
    assert all(r.ok for r in report), report


def test_an_empty_table_is_flagged_empty_not_silently_ok(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Muse P1-6a: verify() passes an emptied table, so the audit must say the table is empty."""
    from argus.journal.db import open_journal

    engine = open_journal(tmp_path / "journal.db")
    report = {entry.table: entry for entry in ledger.audit(engine)}
    assert report["fills"].rows == 0
    assert report["fills"].empty
    assert report["fills"].ok  # an empty table is a warning, not a break


def test_the_audit_command_prints_an_empty_warning(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from typer.testing import CliRunner

    from argus import cli
    from argus.journal import db

    engine = db.open_journal(tmp_path / "journal.db")
    monkeypatch.setattr(db, "open_journal", lambda *a, **k: engine)
    result = CliRunner().invoke(cli.app, ["audit"], terminal_width=200)
    assert "EMPTY" in result.output
