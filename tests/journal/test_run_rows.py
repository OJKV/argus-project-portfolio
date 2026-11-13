"""A run row tells the truth about the run (review J-1 / J-2 / J-N4).

J-1: a failed manager run exited 0, alerted nobody, and — because staleness read MAX(started_at)
of any status — reset the stale-manager clock, so a VPN outage looked like a quiet market.
J-2: a run killed after setup (an exception outside the try, or systemd's SIGTERM at
TimeoutStartSec) left its row RUNNING forever, and nothing reaped it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from argus.journal import db
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.observability import alerts

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Engine:
    return open_journal()  # the default path, shared with the CLI under test


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> RecordingBot:
    recording = RecordingBot()
    monkeypatch.setattr(
        Notifier, "from_settings", classmethod(lambda cls, s=None: cls(recording, "42"))
    )
    return recording


def _row(engine: Engine, kind: str, status: str, started: datetime) -> str:
    run_id = db.new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r, :k, 'demo', :t, :s, 1, 'demo')"
            ),
            {"r": run_id, "k": kind, "t": started.isoformat(), "s": status},
        )
    return run_id


def _status(engine: Engine, run_id: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(text("SELECT status FROM runs WHERE run_id = :r"), {"r": run_id}).scalar()
        )


def test_staleness_counts_only_runs_that_finished_ok(engine: Engine) -> None:
    ok_at = NOW - timedelta(hours=20)
    _row(engine, "manager", "OK", ok_at)
    _row(engine, "manager", "FAILED", NOW - timedelta(hours=1))
    _row(engine, "manager", "HALTED", NOW - timedelta(hours=2))
    _row(engine, "manager", "RUNNING", NOW - timedelta(minutes=5))

    assert alerts.last_run_at(engine, "manager", venue="demo") == ok_at
    assert alerts.stale_manager(alerts.last_run_at(engine, venue="demo"), NOW, 14.0) is not None


def test_a_running_row_past_any_budget_is_marked_abandoned(engine: Engine) -> None:
    ghost = _row(engine, "manager", "RUNNING", datetime.now(UTC) - timedelta(hours=3))
    live = _row(engine, "review", "RUNNING", datetime.now(UTC) - timedelta(minutes=5))

    with start_run(engine, "watchdog", config_hash="-"):
        pass

    assert _status(engine, ghost) == "ABANDONED"
    assert _status(engine, live) == "RUNNING"  # may well be running right now
    with engine.connect() as conn:
        error = conn.execute(
            text("SELECT error FROM runs WHERE run_id = :r"), {"r": ghost}
        ).scalar()
    assert "RUNNING" in str(error)


def test_an_interrupt_still_closes_the_run_row(engine: Engine) -> None:
    with pytest.raises(KeyboardInterrupt), start_run(engine, "review", config_hash="-") as run_id:
        raise KeyboardInterrupt
    assert _status(engine, run_id) == "FAILED"


def test_a_failed_run_alerts(engine: Engine, bot: RecordingBot) -> None:
    with pytest.raises(RuntimeError), start_run(engine, "watchdog", config_hash="-") as run_id:
        raise RuntimeError("OKX <down>")
    assert _status(engine, run_id) == "FAILED"
    assert any("run_failed_watchdog" in text_ for _, text_ in bot.sent), bot.sent


def test_a_manager_run_with_errors_exits_non_zero_and_alerts(
    engine: Engine, bot: RecordingBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argus.cli as cli
    import argus.runs.manager as manager_module
    from argus.runs.manager import ManagerResult

    def failed_run(engine_: Any, _deps: Any, shadow: bool = True, **_: Any) -> ManagerResult:
        result = ManagerResult(run_id=_row(engine_, "manager", "FAILED", NOW), shadow=shadow)
        result.errors.append("ConnectError: VPN down")
        return result

    monkeypatch.setattr(manager_module, "run_manager", failed_run)
    monkeypatch.setattr(cli, "_listed_assets", lambda: [])
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)

    result = CliRunner().invoke(cli.app, ["manager", "--shadow", "--equity", "1000"])

    assert isinstance(result.exception, SystemExit), repr(result.exception)  # not a crash
    assert result.exit_code == 1, result.output
    assert any("run_failed_manager" in text_ for _, text_ in bot.sent), bot.sent


def test_a_repeated_symbol_is_researched_once(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--symbols BTC,btc,BTC` journalled three dossiers and three plans for one asset (J-12)."""
    import argus.cli as cli
    import argus.runs.manager as manager_module
    from argus.runs.manager import ManagerResult

    seen: list[str] = []

    def record(engine_: Any, deps: Any, shadow: bool = True, **_: Any) -> ManagerResult:
        seen.extend(asset.symbol for asset in deps.assets())
        return ManagerResult(run_id=_row(engine_, "manager", "OK", NOW), shadow=shadow)

    monkeypatch.setattr(manager_module, "run_manager", record)
    monkeypatch.setattr(cli, "_listed_assets", lambda: [])
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)

    CliRunner().invoke(
        cli.app, ["manager", "--shadow", "--equity", "1000", "--symbols", "BTC, btc,ETH,BTC"]
    )
    assert seen == ["BTC", "ETH"]


def test_a_manager_run_that_raises_still_alerts(
    engine: Engine, bot: RecordingBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything that escapes run_manager is a failed run the operator hears about, not only a
    traceback in the unit log (J-1 / O-5)."""
    import argus.cli as cli
    import argus.runs.manager as manager_module

    def crash(*_: Any, **__: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(manager_module, "run_manager", crash)
    monkeypatch.setattr(cli, "_listed_assets", lambda: [])
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)

    result = CliRunner().invoke(cli.app, ["manager", "--shadow", "--equity", "1000"])

    assert isinstance(result.exception, RuntimeError), repr(result.exception)  # still exits 1
    assert result.exit_code == 1
    assert any("run_failed_manager" in text_ for _, text_ in bot.sent), bot.sent


def test_a_reaped_run_is_reported_not_only_logged(engine: Engine, bot: RecordingBot) -> None:
    """An ABANDONED row means a process died mid-run. A log line nobody reads is not enough."""
    ghost = _row(engine, "manager", "RUNNING", datetime.now(UTC) - timedelta(hours=3))

    with start_run(engine, "watchdog", config_hash="-"):
        pass

    assert _status(engine, ghost) == "ABANDONED"
    reported = [text_ for _, text_ in bot.sent if "run_abandoned" in text_]
    assert len(reported) == 1 and ghost[:8] in reported[0] and "manager" in reported[0], bot.sent
