"""The ledger stays one verifiable chain whoever writes to it (review J-6 / J-N7 / J-7)."""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, text

from argus.journal import ledger
from argus.journal.db import make_engine, open_journal, start_run


@pytest.fixture
def journal(tmp_path: Path) -> Path:
    path = tmp_path / "journal.db"
    open_journal(path)
    return path


def _run(engine: Engine) -> str:
    with start_run(engine, "test", config_hash="-") as run_id:
        return run_id


def test_two_writers_cannot_fork_the_chain(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reflect, the UI and review's post-mortems write without the run lock (J-N7).

    The window between reading the tail and inserting is widened on purpose: two writers that
    both read the same tail would chain two rows to one parent, and the audit could never again
    tell that from tampering.
    """
    run_id = _run(make_engine(journal))
    real_last_hash = ledger.last_hash

    def slow_last_hash(conn: Connection, table: str) -> str:
        tail = real_last_hash(conn, table)
        time.sleep(0.05)
        return tail

    monkeypatch.setattr(ledger, "last_hash", slow_last_hash)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(name: str) -> None:
        engine = make_engine(journal)  # its own connection pool, like a second process
        start.wait()
        try:
            for i in range(5):
                ledger.append(
                    engine,
                    "position_events",
                    {
                        "run_id": run_id,
                        "symbol": "BTC",
                        "machine": "position",
                        "to_state": "OPEN",
                        "reason": f"{name}-{i}",
                    },
                )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    engine = make_engine(journal)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*), COUNT(DISTINCT prev_hash) FROM position_events"))
        count, parents = rows.one()
    assert count == 10
    assert parents == 10  # every row has its own parent: one chain, no fork
    assert ledger.verify(engine, "position_events") == (True, None)


def test_a_bool_is_hashed_as_the_integer_sqlite_stores(journal: Path) -> None:
    """`True` hashed as `true` but read back as `1`, so a legitimate row looked tampered (J-7)."""
    engine = make_engine(journal)
    run_id = _run(engine)
    ledger.append(
        engine,
        "kill_switches",
        {"run_id": run_id, "scope": "portfolio", "rule": "dd", "triggered": True, "detail": "x"},
    )
    assert ledger.verify(engine, "kill_switches") == (True, None)


def test_decimal_money_is_stored_and_hashed_as_text(journal: Path) -> None:
    engine = make_engine(journal)
    run_id = _run(engine)
    order_id = ledger.append(
        engine,
        "orders",
        {
            "cl_ord_id": "c1",
            "run_id": run_id,
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "kind": "entry",
            "order_type": "limit",
            "price": Decimal("70000.10"),
            "size": Decimal("0.50"),
            "state": "NEW",
            "raw_request": "{}",
        },
    )
    with engine.connect() as conn:
        size = conn.execute(
            text("SELECT size FROM orders WHERE order_id = :o"), {"o": order_id}
        ).scalar()
    assert size == "0.50"  # trailing zero kept: it is the venue's lot precision
    assert ledger.verify(engine, "orders") == (True, None)


@pytest.mark.parametrize(
    ("column", "value"),
    [("size", 0.5), ("price", 1e20), ("size", True), ("step", "3"), ("step", 2.0)],
)
def test_a_value_sqlite_would_store_differently_is_refused(
    journal: Path, column: str, value: object
) -> None:
    """A float in a TEXT column reads back as SQLite's spelling of it, not Python's."""
    engine = make_engine(journal)
    run_id = _run(engine)
    table = "plans" if column == "step" else "orders"
    payload: dict[str, object] = (
        {"run_id": run_id, "mode": "demo", "symbol": "BTC", "state": "GATED", "payload": "{}"}
        if table == "plans"
        else {
            "cl_ord_id": "c2",
            "run_id": run_id,
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "kind": "entry",
            "order_type": "limit",
            "size": "1",
            "state": "NEW",
            "raw_request": "{}",
        }
    )
    payload[column] = value
    with pytest.raises(TypeError, match=column):
        ledger.append(engine, table, payload)
