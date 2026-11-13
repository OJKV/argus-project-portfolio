"""Alert rules are pure, so they are tested without a clock, a network or Logfire."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from argus.journal.db import new_id, open_journal
from argus.observability import alerts


@pytest.fixture
def engine(tmp_path):
    return open_journal(tmp_path / "journal.db")


def test_run_inside_budget_is_silent() -> None:
    assert alerts.run_over_budget("manager", 71.0, 480.0) is None


def test_run_over_budget_names_both_numbers() -> None:
    alert = alerts.run_over_budget("manager", 512.0, 480.0)
    assert alert is not None
    assert alert.severity == "warning"
    assert "512s" in alert.message and "480s" in alert.message


def test_unprotected_position_is_critical() -> None:
    assert alerts.unprotected_positions(0) is None
    alert = alerts.unprotected_positions(2)
    assert alert is not None and alert.severity == "critical"


def test_breaker_alert_lists_hosts_sorted() -> None:
    assert alerts.breaker_open([]) is None
    alert = alerts.breaker_open(["okx", "coingecko"])
    assert alert is not None and "coingecko, okx" in alert.message


def test_llm_error_rate_ignores_small_samples() -> None:
    # 3 of 4 is a 75% failure rate, but four calls say nothing about an hour's health.
    assert alerts.llm_error_rate(4, 3) is None
    assert alerts.llm_error_rate(20, 3) is None
    assert alerts.llm_error_rate(20, 6) is not None


def test_stale_manager_uses_the_gap_not_the_wall_clock() -> None:
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    assert alerts.stale_manager(now - timedelta(hours=13), now, 14) is None
    assert alerts.stale_manager(now - timedelta(hours=15), now, 14) is not None
    assert alerts.stale_manager(None, now, 14) is not None


def test_last_run_at_reads_the_journal(engine) -> None:
    assert alerts.last_run_at(engine, venue="demo") is None
    with engine.begin() as conn:
        for started in ("2026-09-17T10:00:00+00:00", "2026-09-18T09:30:00+00:00"):
            conn.execute(
                text(
                    "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                    " VALUES (:r, 'manager', 'demo', :t, 'OK', 1, 'demo')"
                ),
                {"r": new_id(), "t": started},
            )
    latest = alerts.last_run_at(engine, venue="demo")
    assert latest is not None and latest.hour == 9


class _ExplodingNotifier:
    def send(self, text: str) -> bool:
        raise RuntimeError("telegram down")


def test_dispatch_survives_a_broken_notifier() -> None:
    live = alerts.dispatch([alerts.unprotected_positions(1), None], notifier=_ExplodingNotifier())
    assert [a.key for a in live] == ["positions_unprotected"]


def test_dispatch_with_nothing_to_say_sends_nothing() -> None:
    assert alerts.dispatch([None, None], notifier=_ExplodingNotifier()) == []
