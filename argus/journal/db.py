"""SQLite (WAL) journal: connection, migrations, run context (ARCHITECTURE.md §10).

SQLAlchemy Core with ANSI types only and ISO-UTC TEXT timestamps, so moving to
PostgreSQL is a connection-string change (MEMORY_AND_STATE.md §3).
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, create_engine, event, text

from argus.config import REPO_ROOT, get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

log = structlog.get_logger(__name__)


def utc_now() -> str:
    """ISO-8601 UTC, second precision — the only timestamp format in the journal."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cur = dbapi_connection.cursor()
    # The timeout first: switching to WAL needs a lock, and without a timeout a second process
    # opening the journal at the same moment fails at once instead of waiting its turn (J-5).
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=FULL")  # a trade journal may not lose the last commit
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def _journal_file(path: Path | None) -> Path:
    db_path = path or get_settings().journal_path
    return db_path if db_path.is_absolute() else (REPO_ROOT / db_path).resolve()


def _readonly_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA query_only=ON")
    cur.close()


def open_journal_readonly(path: Path | None = None) -> Engine:
    """The journal for commands that only read it, such as `argus replay` (review J-10).

    Never creates the file and never migrates it: replaying an old copy for an investigation must
    not change the evidence, and pointing at the wrong ARGUS_HOME must fail rather than leave an
    empty journal behind. `query_only` makes SQLite itself refuse any write on these connections.
    """
    db_path = _journal_file(path)
    if not db_path.is_file():
        raise FileNotFoundError(f"no journal at {db_path}")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    event.listen(engine, "connect", _readonly_pragmas)
    return engine


def make_engine(path: Path | None = None) -> Engine:
    """Engine for the journal at `path` (default: ARGUS_HOME/journal.db). Creates parents."""
    db_path = _journal_file(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    event.listen(engine, "connect", _sqlite_pragmas)
    return engine


@contextmanager
def write_transaction(engine: Engine) -> Iterator[Connection]:
    """A transaction that holds SQLite's write lock from its first statement (`BEGIN IMMEDIATE`).

    pysqlite's default opens a transaction only at the first INSERT/UPDATE/DELETE, so DDL before
    it autocommits one statement at a time and a SELECT before it reads outside any lock. Both
    bit us: a migration that failed half-way left its first tables behind (J-5), and two ledger
    writers could read the same chain tail and fork it (J-6). Taking the lock up front makes the
    whole block one atomic unit, and a second writer waits on `busy_timeout` instead of racing.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def _statements(script: str) -> Iterator[str]:
    """Split a migration into statements the way SQLite parses them.

    `split(";\\n")` cut a trigger's `BEGIN … END` body at its first inner semicolon, and the
    ledger guards are triggers. `sqlite3.complete_statement` knows about bodies, strings and
    comments, so a statement ends where SQLite says it does.
    """
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer
            buffer = ""
    if buffer.strip():
        yield buffer  # an unterminated tail is still executed, so its error is reported


def applied_migrations(conn: Connection) -> set[str]:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT)"
        )
    )
    # Journals created before checksums existed need the column before anything reads it.
    columns = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(schema_migrations)")}
    if "checksum" not in columns:
        conn.exec_driver_sql("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")
    rows = conn.execute(text("SELECT name FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _needs_migrating(engine: Engine) -> bool:
    """Whether `migrate` has anything to do — new files, a changed file, or old bookkeeping.

    A plain read with no transaction, so it never waits on another writer.
    """
    with engine.connect() as conn:
        tables = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).first()
        if tables is None:
            return True
        columns = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(schema_migrations)")}
        if "checksum" not in columns:
            return True
        recorded: dict[str, str | None] = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT name, checksum FROM schema_migrations"))
        }
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name not in recorded:
            return True
        if recorded[path.name] and recorded[path.name] != _checksum(path):
            return True  # the locked pass raises the immutability error
    return False


def schema_is_current(engine: Engine) -> bool:
    """True when no migration is pending. A plain read; never applies anything."""
    return not _needs_migrating(engine)


def migrate(engine: Engine) -> list[str]:
    """Apply pending .sql migrations in filename order. Returns the ones applied.

    Applied files are checksummed. Editing one that has already run is invisible — the runner
    tracks filenames, so the new statements never execute on a machine that ran the old version,
    and two deployments silently diverge. That cost a live LLM run once (F-085); now it raises.

    All pending migrations apply in one `BEGIN IMMEDIATE` transaction: either every statement
    lands or none does, and a second process opening the journal after a deploy waits for the
    first and then finds nothing left to apply (J-5). A failure therefore leaves the schema
    exactly as it was, which the code already deployed can still run against.

    The lock is taken only when something is pending, and the check is repeated under it. Opening
    a current journal is a read: the watchdog opens it before it verifies any stop, and a writer
    holding the lock past busy_timeout must not end that tick before reconcile.
    """
    if not _needs_migrating(engine):
        return []
    applied: list[str] = []
    with write_transaction(engine) as conn:
        done = applied_migrations(conn)
        recorded: dict[str, str | None] = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT name, checksum FROM schema_migrations"))
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                previous = recorded.get(path.name)
                if previous and previous != _checksum(path):
                    raise RuntimeError(
                        f"{path.name} changed after it was applied — a migration that has run is "
                        "history. Add a new migration instead of editing this one."
                    )
                continue
            for statement in _statements(path.read_text(encoding="utf-8")):
                # exec_driver_sql: DDL is raw, so ":8" inside a comment is not a bind param
                conn.exec_driver_sql(statement)
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (name, applied_at, checksum) VALUES (:n, :t, :c)"
                ),
                {"n": path.name, "t": utc_now(), "c": _checksum(path)},
            )
            applied.append(path.name)
    return applied


def reconcile_migrations(engine: Engine) -> list[str]:
    """Re-record checksums for applied migrations whose files have changed. Returns their names.

    The immutability guard is strict on purpose, but a strict rule with no escape gets bypassed
    by turning it off. This is the deliberate way through: it changes no schema, touches only the
    bookkeeping, and names every file it re-blesses so the decision is visible in the log.
    """
    reconciled: list[str] = []
    with engine.begin() as conn:
        recorded: dict[str, str | None] = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT name, checksum FROM schema_migrations"))
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            current = _checksum(path)
            if path.name in recorded and recorded[path.name] != current:
                conn.execute(
                    text("UPDATE schema_migrations SET checksum = :c WHERE name = :n"),
                    {"c": current, "n": path.name},
                )
                reconciled.append(path.name)
    return reconciled


def open_journal(path: Path | None = None) -> Engine:
    """Engine with migrations applied — the normal entry point."""
    engine = make_engine(path)
    migrate(engine)
    return engine


# A RUNNING row older than this has no process behind it. systemd kills a run at its unit's
# TimeoutStartSec: 900 s for manager/review, 960 s for the watchdog, 1500 s for reflect (600 s of
# it waiting for deploy.lock), and 4200 s for eval-models (which writes no run row). Two hours
# exceeds them all, so the reaper never touches a run that is genuinely still going. Housekeeping,
# not a risk threshold.
ABANDON_AFTER = timedelta(hours=2)


def reap_abandoned(engine: Engine, now: datetime | None = None) -> list[tuple[str, str]]:
    """Mark RUNNING rows older than ABANDON_AFTER as ABANDONED. Returns their (run_id, kind).

    A run killed after its row was written — an exception outside the manager's `try`, or the
    SIGTERM systemd sends at TimeoutStartSec, which Python does not turn into an exception — left
    its row RUNNING forever, and the dashboard showed a run in progress that had died (J-2).
    """
    moment = now or datetime.now(UTC)
    cutoff = (moment - ABANDON_AFTER).isoformat()
    with engine.begin() as conn:
        ghosts = [
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                text("SELECT run_id, kind FROM runs WHERE status = 'RUNNING' AND started_at < :c"),
                {"c": cutoff},
            )
        ]
        if ghosts:
            conn.execute(
                text(
                    "UPDATE runs SET status = 'ABANDONED', finished_at = :t, error = :e"
                    " WHERE status = 'RUNNING' AND started_at < :c"
                ),
                {
                    "t": utc_now(),
                    "e": f"still RUNNING after {ABANDON_AFTER}; its process died without closing",
                    "c": cutoff,
                },
            )
    return ghosts


@contextmanager
def start_run(engine: Engine, kind: str, config_hash: str, shadow: bool = False) -> Iterator[str]:
    """Open a `runs` row, yield its run_id, and close it OK/FAILED on the way out.

    Every run first reaps rows abandoned by dead processes, so a ghost lasts at most until the
    next watchdog tick past ABANDON_AFTER. A failure raises an alert as well as closing the row:
    a FAILED row nobody is told about is how a dead run looks like a quiet market (J-1).
    """
    settings = get_settings()
    try:
        reaped = reap_abandoned(engine)
        if reaped:
            log.warning("runs.reaped_abandoned", run_ids=[r[:8] for r, _ in reaped])
            from argus.observability import alerts

            # No cooldown: the reaper flips each row once, so this is one message per dead run,
            # and a second death the same afternoon must not be held back behind the first.
            alerts.dispatch([alerts.runs_abandoned(reaped)])
    except Exception as exc:  # noqa: BLE001 - housekeeping must never stop a run
        log.warning("runs.reap_failed", error=f"{type(exc).__name__}: {exc}")
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash) "
                "VALUES (:r, :k, :m, :t, 'RUNNING', :s, :c)"
            ),
            {
                "r": run_id,
                "k": kind,
                "m": settings.mode,
                "t": utc_now(),
                "s": int(shadow),
                "c": config_hash,
            },
        )
    structlog.contextvars.bind_contextvars(run_id=run_id)
    try:
        yield run_id
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _close(engine, run_id, "FAILED", error)
        from argus.observability import alerts

        alerts.dispatch([alerts.run_failed(kind, run_id, error)], engine=engine)
        raise
    except BaseException as exc:
        # Ctrl-C or SystemExit: not an alertable failure, but the row must not stay RUNNING.
        _close(engine, run_id, "FAILED", f"interrupted: {type(exc).__name__}")
        raise
    else:
        _close(engine, run_id, "OK", None)
    finally:
        structlog.contextvars.unbind_contextvars("run_id")


def record_skipped_run(engine: Engine, kind: str, reason: str) -> str:
    """Journal a run that never started because another held the run lock (J-4).

    Without a row a skipped watchdog tick left no trace at all — the run lock exists so two runs
    never touch orders at once, not so one of them can vanish from the record.
    """
    run_id = new_id()
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, finished_at, status, shadow,"
                " config_hash, error) VALUES (:r, :k, :m, :t, :t, 'SKIPPED', 0, '-', :e)"
            ),
            {"r": run_id, "k": kind, "m": get_settings().mode, "t": now, "e": reason[:2000]},
        )
    return run_id


def consecutive_skips(engine: Engine, kind: str) -> int:
    """How many of the latest `kind` runs in a row were SKIPPED."""
    with engine.connect() as conn:
        statuses = conn.execute(
            text(
                "SELECT status FROM runs WHERE kind = :k ORDER BY started_at DESC, rowid DESC"
                " LIMIT 50"
            ),
            {"k": kind},
        ).all()
    count = 0
    for (status,) in statuses:
        if status != "SKIPPED":
            break
        count += 1
    return count


def _close(engine: Engine, run_id: str, status: str, error: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = :s, finished_at = :t, error = :e WHERE run_id = :r"),
            {"s": status, "t": utc_now(), "e": error[:2000] if error else None, "r": run_id},
        )
