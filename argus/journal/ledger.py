"""Insert-only ledger with a SHA-256 hash chain per table (MEMORY_AND_STATE.md §4).

Corrections are new rows carrying `supersedes_id`; nothing is ever updated or deleted — the
database enforces that with triggers (migration 010), and `argus audit` walks each chain, checks
the triggers are still there, and reports the first row whose hash does not verify.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, text

from argus.journal.db import new_id, utc_now, write_transaction

# Tables that are append-only and hash-chained (ARCHITECTURE.md §10).
LEDGER_TABLES: dict[str, str] = {
    "plans": "plan_id",
    "gate_results": "gate_result_id",
    "orders": "order_id",
    "fills": "fill_id",
    "position_events": "event_id",
    "kill_switches": "kill_switch_id",
}

GENESIS = "0" * 64

# Columns filled in once after the INSERT, and therefore outside the row hash. The venue's ids do
# not exist until it answers, so `record_venue_ids` writes them into the order row afterwards
# (F-129). While they were hashed, that one UPDATE broke the `orders` chain for every placed order
# (J-N1). They are hashed as NULL — which is what every row held when it was appended, so chains
# written before this change still verify — and the `orders_write_once` trigger protects them
# instead: NULL may become a value, a value may never change.
WRITE_ONCE: dict[str, frozenset[str]] = {
    "orders": frozenset({"exchange_id", "algo_id", "raw_response"}),
}

# The triggers that make each table insert-only (migration 010). A missing one is an audit
# failure: dropping a guard is the first step of editing history.
GUARDS: dict[str, tuple[str, str]] = {
    table: (
        f"{table}_write_once" if table in WRITE_ONCE else f"{table}_no_update",
        f"{table}_no_delete",
    )
    for table in LEDGER_TABLES
}


def row_hash(payload: dict[str, Any], prev_hash: str) -> str:
    """SHA-256 over canonical JSON of the row plus the previous hash."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}{body}".encode()).hexdigest()


def _hashed(table: str, row: dict[str, Any]) -> dict[str, Any]:
    """What the chain covers: every column but the hashes, write-once columns as NULL."""
    exempt = WRITE_ONCE.get(table, frozenset())
    hashed = {k: v for k, v in row.items() if k not in ("prev_hash", "row_hash")}
    return {k: (None if k in exempt else v) for k, v in hashed.items()}


def last_hash(conn: Connection, table: str) -> str:
    _assert_ledger(table)
    row = conn.execute(
        text(f"SELECT row_hash FROM {table} ORDER BY rowid DESC LIMIT 1")  # noqa: S608
    ).fetchone()
    return row[0] if row else GENESIS


def _column_types(conn: Connection, table: str) -> dict[str, str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    return {r[1]: str(r[2]).upper() for r in rows}


def _as_stored(table: str, column: str, declared: str, value: Any) -> Any:
    """The value exactly as SQLite will hand it back, or TypeError.

    The hash is computed in Python before the INSERT and recomputed by `verify` from what SQLite
    returns. Anything SQLite stores differently from how Python spells it makes a legitimate row
    look tampered: `True` hashes as `true` and reads back `1`; a float in a TEXT column reads back
    as SQLite's rendering of it, which is not always Python's (J-7). So only values that
    round-trip byte-for-byte are allowed: str in TEXT, int in INTEGER. A bool becomes the int
    SQLite stores, and a Decimal or int bound for TEXT becomes the text SQLite would store. Floats
    are refused outright — money is never a float here, and a float elsewhere is a caller's bug.
    """
    if value is None:
        return None
    if "INT" in declared:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
    elif isinstance(value, str):
        return value
    elif isinstance(value, Decimal) or (isinstance(value, int) and not isinstance(value, bool)):
        return str(value)
    raise TypeError(
        f"{table}.{column} ({declared or 'untyped'}) cannot hold {type(value).__name__} "
        f"{value!r} without changing it on the way back; pass str or int"
    )


def append(engine: Engine, table: str, payload: dict[str, Any]) -> str:
    """Insert one ledger row, chaining it to the previous. Returns the new row's id.

    Every column of the table is written explicitly (missing ones as NULL) so the hashed
    body is byte-for-byte what `verify` later reads back.

    Reading the tail and inserting the new row happen under one `BEGIN IMMEDIATE`. The run lock
    only serialises manager, review and watchdog; reflect, the UI and review's post-mortem writes
    run outside it, and two writers that both read the same tail would chain two rows to one
    parent — a fork the audit can never again tell apart from tampering (J-6 / J-N7).
    """
    _assert_ledger(table)
    pk = LEDGER_TABLES[table]
    body = dict(payload)
    body.setdefault(pk, new_id())
    body.setdefault("created_at", utc_now())
    body.pop("prev_hash", None)
    body.pop("row_hash", None)
    with write_transaction(engine) as conn:
        declared = _column_types(conn, table)
        unknown = set(body) - set(declared)
        if unknown:
            raise ValueError(f"{table} has no column(s) {sorted(unknown)}")
        row = {col: _as_stored(table, col, kind, body.get(col)) for col, kind in declared.items()}
        prev = last_hash(conn, table)
        row["prev_hash"] = prev
        row["row_hash"] = row_hash(_hashed(table, row), prev)
        columns = ", ".join(row)
        params = ", ".join(f":{k}" for k in row)
        conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({params})"), row)  # noqa: S608
    return str(row[pk])


def verify(engine: Engine, table: str) -> tuple[bool, str | None]:
    """Walk the chain. Returns (ok, first_broken_row_id).

    An empty table returns (True, None): there is nothing to contradict. That is not evidence the
    table was never truncated; `audit` reports such tables as empty so a human can compare the row
    count and head hash kept off the box.
    """
    _assert_ledger(table)
    pk = LEDGER_TABLES[table]
    prev = GENESIS
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY rowid")).mappings()  # noqa: S608
        for row in rows:
            expected = row_hash(_hashed(table, dict(row)), prev)
            if row["prev_hash"] != prev or row["row_hash"] != expected:
                return False, str(row[pk])
            prev = str(row["row_hash"])
    return True, None


def verify_all(engine: Engine) -> dict[str, tuple[bool, str | None]]:
    return {table: verify(engine, table) for table in LEDGER_TABLES}


def last_hash_of(engine: Engine, table: str) -> str:
    with engine.connect() as conn:
        return last_hash(conn, table)


@dataclass
class TableAudit:
    table: str
    rows: int
    chain_ok: bool
    broken_id: str | None
    head: str  # the newest row_hash: kept off the box, it shows any later rewrite of history
    missing_guards: list[str] = field(default_factory=list)
    altered_guards: list[str] = field(default_factory=list)  # present, but not the migration's

    @property
    def empty(self) -> bool:
        """No rows, so the chain check proved nothing. A warning, not a failure: a table that has
        never been written to is normal, but an emptied one verifies identically (review P1-6)."""
        return self.rows == 0

    @property
    def ok(self) -> bool:
        return self.chain_ok and not self.missing_guards and not self.altered_guards


def _expected_guards() -> dict[str, str]:
    """Each trigger's text as SQLite stores it, from the migrations themselves.

    Built by applying the migrations to an in-memory database, so the comparison is exact without
    parsing SQL: SQLite stores the CREATE statement as given, and the checksum guard in `migrate`
    keeps the files identical to what the journal ran.
    """
    import sqlite3

    from argus.journal import db

    memory = sqlite3.connect(":memory:")
    try:
        for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
            for statement in db._statements(path.read_text(encoding="utf-8")):
                memory.execute(statement)
        return dict(memory.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))
    finally:
        memory.close()


def audit(engine: Engine) -> list[TableAudit]:
    """Every ledger table: does its chain verify, and are its insert-only triggers in place?

    The chain alone cannot see a truncated tail — an emptied table verifies as (True, None) — and
    the triggers alone cannot see an edit made after someone dropped them. Together, with the
    head hash and row count copied somewhere else, they can (J-3). A guard counts only if its text
    is the migration's: a trigger of the same name that does nothing is not a guard.
    """
    expected = _expected_guards()
    with engine.connect() as conn:
        present = {
            str(r[0]): r[1]
            for r in conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        counts = {
            t: int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t}").scalar() or 0)  # noqa: S608
            for t in LEDGER_TABLES
        }
        heads = {t: last_hash(conn, t) for t in LEDGER_TABLES}
    report = []
    for table in LEDGER_TABLES:
        ok, broken = verify(engine, table)
        report.append(
            TableAudit(
                table=table,
                rows=counts[table],
                chain_ok=ok,
                broken_id=broken,
                head=heads[table],
                missing_guards=[g for g in GUARDS[table] if g not in present],
                # Checking names alone passed a guard dropped, the tail deleted, and a no-op
                # recreated under the same name.
                altered_guards=[
                    g for g in GUARDS[table] if g in present and present[g] != expected.get(g)
                ],
            )
        )
    return report


def _assert_ledger(table: str) -> None:
    if table not in LEDGER_TABLES:
        raise ValueError(f"{table} is not a ledger table: {sorted(LEDGER_TABLES)}")
