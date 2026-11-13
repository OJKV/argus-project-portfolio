"""The 30-day equity peak the drawdown kill-switch measures against.

Review 2026-09-18 X-18: the window compared the journal's ISO timestamps (`2026-09-07T11:00:00`)
with SQLite's `datetime('now', '-30 days')` (`2026-09-07 12:00:00`). `T` sorts after a space, so
every snapshot from the boundary *day* counted, hours older than 30 days included.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.config import LIVE_VENUE_WORD, Settings
from argus.exchange.fake import FakeExchange
from argus.journal.db import new_id, open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog, equity_peak_30d


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _snapshot(engine: Engine, run_id: str, at: datetime, equity: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO equity_snapshots (snapshot_id, run_id, created_at, equity)"
                " VALUES (:i, :r, :t, :e)"
            ),
            {
                "i": new_id(),
                "r": run_id,
                "t": at.replace(microsecond=0).isoformat(),
                "e": equity,
            },
        )


def test_a_snapshot_older_than_thirty_days_is_outside_the_window(engine: Engine) -> None:
    with start_run(engine, "watchdog", "demo") as run_id:
        stale = datetime.now(UTC) - timedelta(days=30, minutes=1)
        _snapshot(engine, run_id, stale, "2000")
    assert equity_peak_30d(engine, Decimal("1000"), venue="demo") == Decimal("1000")


def test_a_snapshot_inside_the_window_sets_the_peak(engine: Engine) -> None:
    with start_run(engine, "watchdog", "demo") as run_id:
        _snapshot(engine, run_id, datetime.now(UTC) - timedelta(days=29), "2000")
    assert equity_peak_30d(engine, Decimal("1000"), venue="demo") == Decimal("2000")


# --- review 2026-09-18 X-3 (the parts still open on 2026-10-07) -------------------------------


def _watchdog(
    ex: FakeExchange, engine: Engine, tmp_path: Path, settings: Settings | None = None
) -> Watchdog:
    return Watchdog(
        exchange=ex,
        engine=engine,
        notifier=Notifier(bot=RecordingBot(), chat_id="42"),
        settings=settings or Settings(mode="demo", argus_home=tmp_path),
    )


def test_a_demo_peak_never_judges_a_live_account(engine: Engine, tmp_path: Path) -> None:
    """A journal used on demo ($5,000) and then live ($150) fired the kill on the first live tick.

    Runs are tagged with the venue (`config_hash`, F-131); the peak must only read its own. The
    watchdog is configured for the live account, as the run it writes under says (F-149).
    """
    with start_run(engine, "watchdog", "demo") as demo_run:
        _snapshot(engine, demo_run, datetime.now(UTC) - timedelta(days=1), "5000")
    ex = FakeExchange(equity=Decimal("150"))
    live = Settings(  # type: ignore[call-arg]
        _env_file=None,
        mode="auto",
        live_trading="I_UNDERSTAND",
        okx_demo=False,
        live_venue=LIVE_VENUE_WORD,
        argus_home=tmp_path,
    )
    with start_run(engine, "watchdog", live.venue) as live_run:
        result = _watchdog(ex, engine, tmp_path, live).run_once(live_run)
    assert not result.killed
    assert result.drawdown_pct == Decimal("0")


def test_one_glitchy_balance_reading_does_not_flatten_the_account(
    engine: Engine, tmp_path: Path
) -> None:
    """X-3: a single breaching read is re-checked before close-all + HALT."""
    ex = FakeExchange(equity=Decimal("1000"))
    ex.open_unprotected("BTC-USDT-SWAP")
    ex.amend_stop("BTC-USDT-SWAP", Decimal("70000"))
    wd = _watchdog(ex, engine, tmp_path)
    with start_run(engine, "watchdog", "demo") as run_id:
        wd.run_once(run_id)  # peak 1000

    readings = iter([Decimal("700"), Decimal("1000")])
    real = ex.fetch_balance

    def glitch_then_truth() -> dict[str, Any]:
        return {**real(), "equity": next(readings)}

    ex.fetch_balance = glitch_then_truth  # type: ignore[method-assign]
    with start_run(engine, "watchdog", "demo") as run_id:
        result = wd.run_once(run_id)
    assert not result.killed
    assert ex.fetch_positions(), "nothing may be closed on one reading"
