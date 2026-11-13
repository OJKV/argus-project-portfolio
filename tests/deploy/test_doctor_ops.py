"""`argus doctor` checks what an armed box needs (review 2026-10-07, J-N13).

Three gaps: Telegram was optional even when armed — the channel every alert travels on (F-141's
364 unread alerts); there was no check that the scheduled manager actually runs; and the universe
check failed unless the core list had exactly twelve entries, a count nobody maintains.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from argus import config, doctor

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "approve")
    monkeypatch.setenv("LIVE_TRADING", "I_UNDERSTAND")
    config.get_settings.cache_clear()


def _rows(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


# ── Telegram ──────────────────────────────────────────────────────────────────────────────────


def test_telegram_is_required_when_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    rows = _rows(doctor.check_keys())
    assert not rows["Telegram token"].ok
    assert not rows["Telegram chat"].ok


def test_telegram_stays_optional_for_research(monkeypatch: pytest.MonkeyPatch) -> None:
    config.get_settings.cache_clear()
    rows = _rows(doctor.check_keys())
    assert rows["Telegram token"].ok and "optional" in rows["Telegram token"].detail


# ── the scheduled manager actually runs ──────────────────────────────────────────────────────


def _journal(
    home: Path, started: datetime | None, *, venue: str = "demo", status: str = "OK"
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "journal.db") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs"
            " (run_id TEXT, kind TEXT, mode TEXT, started_at TEXT, status TEXT, config_hash TEXT)"
        )
        if started is not None:
            conn.execute(
                "INSERT INTO runs VALUES (?, 'manager', 'approve', ?, ?, ?)",
                (f"r-{venue}-{status}", started.isoformat(), status, venue),
            )


def _arm_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real-money account, armed. Settings only: `check_runs` reads just the journal."""
    monkeypatch.setenv("OKX_DEMO", "false")
    monkeypatch.setenv("LIVE_VENUE", config.LIVE_VENUE_WORD)
    _arm(monkeypatch)
    assert config.get_settings().venue == "live"


def test_a_demo_run_or_a_failed_run_does_not_keep_the_live_doctor_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-153: doctor read MAX(started_at) over every account and every status, so a demo run, or
    a live run that FAILED, passed it while the live watchdog's manager_stale alert fired."""
    _arm_live(monkeypatch)
    home = config.get_settings().argus_home
    _journal(home, NOW - timedelta(hours=3), venue="demo", status="OK")
    _journal(home, NOW - timedelta(hours=1), venue="live", status="FAILED")
    (row,) = doctor.check_runs(now=NOW)
    assert not row.ok
    assert row.detail == "no manager run on record"


def test_an_unreadable_journal_fails_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    home = config.get_settings().argus_home
    home.mkdir(parents=True, exist_ok=True)
    (home / "journal.db").write_bytes(b"not a database, not even close" * 10)
    (row,) = doctor.check_runs(now=NOW)
    assert not row.ok
    assert row.detail.startswith("journal unreadable")


def test_a_journal_path_that_is_not_a_file_fails_and_does_not_crash_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`open_journal_readonly` raises FileNotFoundError, not a SQLAlchemyError, and nothing in
    `argus doctor` would catch it (independent review of F-153)."""
    _arm(monkeypatch)
    (config.get_settings().argus_home / "journal.db").mkdir(parents=True)
    (row,) = doctor.check_runs(now=NOW)
    assert not row.ok
    assert row.detail.startswith("journal unreadable")


def test_the_live_doctor_counts_the_live_accounts_own_ok_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm_live(monkeypatch)
    home = config.get_settings().argus_home
    _journal(home, NOW - timedelta(hours=3), venue="live", status="OK")
    _journal(home, NOW - timedelta(hours=1), venue="demo", status="FAILED")
    (row,) = doctor.check_runs(now=NOW)
    assert row.ok, row.detail


def test_an_armed_box_whose_manager_has_not_run_for_a_day_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch)
    _journal(config.get_settings().argus_home, NOW - timedelta(hours=26))
    (row,) = doctor.check_runs(now=NOW)
    assert not row.ok
    assert "26.0h" in row.detail


def test_an_armed_box_with_a_recent_manager_run_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    _journal(config.get_settings().argus_home, NOW - timedelta(hours=3))
    (row,) = doctor.check_runs(now=NOW)
    assert row.ok


def test_an_armed_box_with_no_manager_run_at_all_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    _journal(config.get_settings().argus_home, None)
    (row,) = doctor.check_runs(now=NOW)
    assert not row.ok


def test_staleness_is_information_when_not_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODE=demo never runs the scheduled manager (it exits 2), so a gap is expected there."""
    config.get_settings.cache_clear()
    _journal(config.get_settings().argus_home, NOW - timedelta(days=9))
    (row,) = doctor.check_runs(now=NOW)
    assert row.ok


def test_no_journal_yet_is_not_a_failure_and_creates_nothing() -> None:
    config.get_settings.cache_clear()
    home = config.get_settings().argus_home
    (row,) = doctor.check_runs(now=NOW)
    assert row.ok
    assert not (home / "journal.db").exists()


def test_doctor_runs_the_staleness_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    import argus.cli as cli

    result = CliRunner().invoke(cli.app, ["doctor", "--no-network"])
    assert "last manager run" in result.output


# ── universe ──────────────────────────────────────────────────────────────────────────────────


def test_the_universe_check_does_not_hard_code_a_count(monkeypatch: pytest.MonkeyPatch) -> None:
    eleven = [{"symbol": s, "okx": f"{s}-USDT-SWAP"} for s in "BTC ETH SOL A B C D E F G H".split()]
    monkeypatch.setattr(doctor, "universe_config", lambda: {"core": eleven})
    (row,) = [c for c in doctor.check_config() if c.name == "universe.yaml"]
    assert row.ok, row.detail


def test_a_duplicated_or_empty_core_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    dup = [{"symbol": "BTC", "okx": "BTC-USDT-SWAP"}] * 2
    for core in (dup, []):
        monkeypatch.setattr(doctor, "universe_config", lambda core=core: {"core": core})
        (row,) = [c for c in doctor.check_config() if c.name == "universe.yaml"]
        assert not row.ok
