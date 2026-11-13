"""One journal, two accounts: the reads F-149 left unscoped (F-150).

F-149 scoped the watchdog's money-path reads to the run's own account and listed five more that
still read both. Each test seeds the other account with the row that used to leak in:

* the review's resting-entry sweep took demo entries to the live venue, which does not hold them,
  and the journal marked them CANCELLED;
* the Reviewer was shown a newer demo plan for a live position — another trade's stop and thesis;
* a demo trade closing on the same symbol cut the live post-mortem's window, so the live trade's
  own entry fell outside it and no post-mortem was written;
* a demo orphan page held back the page for a live orphan on the same symbol for 24 hours;
* a demo manager run kept the live stale-manager alert quiet.

Every account here is a fake; the live `Settings` only names the account.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import order_state, record_order, transition_order
from argus.execution.resting import sweep
from argus.journal import positions as positions_journal
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.observability import alerts
from argus.runs.watchdog import Watchdog
from argus.ui import queries
from tests.runs.test_venue_scope import BTC, _entry, _settings

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _now_is_when_the_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """NOW was fixed when this module was imported — at collection, before the whole suite runs.
    The journal stamps rows it writes itself (plans, orders) with the real clock, so in a suite
    that took more than a few minutes to reach this file the rows built from NOW sat minutes
    BEFORE the plan they belong to, and the post-mortem found 'no plan on record'. It passed in
    isolation and in a fast run and failed in a slow one."""
    monkeypatch.setattr(sys.modules[__name__], "NOW", datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _resting(engine: Engine, venue: str, ex: FakeExchange, cl: str) -> str:
    """A BTC limit entry `venue`'s manager placed and that account's venue still holds."""
    order = {
        "symbol": BTC,
        "side": "buy",
        "size": Decimal("1"),
        "order_type": "limit",
        "price": Decimal("100000"),
        "cl_ord_id": cl,
        "attach_algos": [{"kind": "sl", "trigger_px": Decimal("97000")}],
    }
    with start_run(engine, "manager", config_hash=venue) as run_id:
        order_id = record_order(engine, run_id, order)
    ex.place_bracket(order)
    transition_order(engine, order_id, "OPEN")
    return order_id


def _seen(engine: Engine, venue: str, minutes: int) -> str:
    """`venue`'s watchdog sees a BTC long open, `minutes` from now. Returns the snapshot time."""
    at = NOW + timedelta(minutes=minutes)
    with start_run(engine, "watchdog", config_hash=venue) as run_id:
        positions_journal.record(
            engine,
            run_id,
            [{"symbol": BTC, "side": "long", "size": "1", "entry_price": 1, "mark_price": 1}],
            now=at,
        )
    return at.isoformat()


def _closed(engine: Engine, venue: str, minutes: int) -> None:
    """`venue`'s watchdog sees its BTC position gone, `minutes` from now."""
    with start_run(engine, "watchdog", config_hash=venue) as run_id:
        positions_journal.mark_closed(
            engine,
            run_id,
            positions_journal.newly_closed(engine, [], venue=venue),
            now=NOW + timedelta(minutes=minutes),
        )


def _tick(engine: Engine, tmp_path: Path, venue: str, ex: FakeExchange, bot: RecordingBot) -> None:
    settings = _settings(venue, tmp_path)
    with start_run(engine, "watchdog", config_hash=settings.venue) as run_id:
        Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=bot, chat_id="42"),
            settings=settings,
        ).run_once(run_id)


def _held_btc() -> FakeExchange:
    """A venue holding a protected BTC long the journal has no entry for: an orphan."""
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    ex.open_unprotected(BTC, side="long")
    ex.amend_stop(BTC, Decimal("95000"))
    return ex


def _orphan_pages(bot: RecordingBot) -> list[str]:
    return [t for _, t in bot.sent if "Orphan position" in t]


# --- 1. the resting-entry sweep ---------------------------------------------------------------


def test_a_live_sweep_leaves_a_demo_entry_alone(engine: Engine) -> None:
    """The live venue does not hold a demo order. Read across accounts, "gone from the venue"
    marked a demo entry that is still resting on demo CANCELLED, and the demo book went blind."""
    demo = FakeExchange(marks={BTC: Decimal("100000")})
    order_id = _resting(engine, "demo", demo, cl="demo1")
    live = FakeExchange(marks={BTC: Decimal("100000")})

    swept = sweep(
        engine, live, now=NOW + timedelta(hours=13), valid_hours=12, drift_pct=2.0, venue="live"
    )

    assert swept.settled == []
    assert swept.errors == []
    assert order_state(engine, order_id) == "OPEN"


def test_a_live_sweep_still_expires_its_own_entry(engine: Engine) -> None:
    live = FakeExchange(marks={BTC: Decimal("100000")})
    order_id = _resting(engine, "live", live, cl="live1")

    swept = sweep(
        engine, live, now=NOW + timedelta(hours=13), valid_hours=12, drift_pct=2.0, venue="live"
    )

    assert [s.to_state for s in swept.settled] == ["EXPIRED"]
    assert order_state(engine, order_id) == "EXPIRED"


# --- 2. the plan the Reviewer is shown --------------------------------------------------------


def test_the_live_reviewer_is_shown_the_live_plan(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later demo entry on the same asset and side was "the newest placed plan", so the
    Reviewer judged a live position against the demo trade's thesis and its wider stop."""
    from argus.runs import review as module
    from tests.runs.test_review import FakeVenue, _Call, _decide, _dossier

    _entry(engine, "live", stop="95000")
    _entry(engine, "demo", stop="90000")  # later, and wider for a long
    shown: list[dict[str, Any]] = []

    def fake(_r: Any, _id: str, _pos: dict[str, Any], plan: dict[str, Any], *_a: Any) -> Any:
        shown.append(plan)
        return _Call(_decide("HOLD"))

    monkeypatch.setattr(module, "ask_reviewer", fake)
    venue = FakeVenue(
        positions=[{"symbol": BTC, "side": "long"}],
        algos=[{"symbol": BTC, "kind": "sl", "trigger_px": 95000.0}],
    )
    with start_run(engine, "review", config_hash="live") as run_id:
        module.run_review(engine, object(), venue, run_id, _dossier, dry_run=True, venue="live")

    assert [p.get("stop") for p in shown] == [95000.0]


# --- 3. the post-mortem's window --------------------------------------------------------------


def _live_post_mortem(engine: Engine, monkeypatch: pytest.MonkeyPatch, seen_at: str) -> list[str]:
    """The live review writes the post-mortem for a BTC long last seen open at `seen_at`."""
    from argus.llm.router import AllModelsFailedError
    from argus.llm.schemas import PostMortem
    from argus.runs import review as module
    from tests.runs.test_review import _Call

    report = PostMortem(
        process_verdict="sound",
        would_take_again=True,
        entry_timing="good",
        sizing="right",
        stop_placement="right",
        missed_signals=[],
        do_differently="",
        feedback="The thesis held; price reached the stop first.",
    )
    monkeypatch.setattr(module, "AllModelsFailedError", AllModelsFailedError)
    monkeypatch.setattr("argus.llm.agents.postmortem.analyse", lambda *a, **k: _Call(report))

    class Closed:
        symbol, side, entry_price = BTC, "long", 100000.0
        last_seen = {"symbol": BTC, "created_at": seen_at, "mark_price": 95000.0}

    with start_run(engine, "review", config_hash="live") as run_id:
        return module.write_postmortems(engine, object(), run_id, [Closed()], venue="live")


def test_a_demo_trade_closing_does_not_cut_the_live_post_mortems_window(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window opens at the previous trade on the symbol. Read across accounts, a demo trade
    that opened and closed while the live one ran became "the previous trade", the live entry
    fell before the window, and the live close got no post-mortem at all."""
    _entry(engine, "live", stop="95000")
    _seen(engine, "live", minutes=1)
    _seen(engine, "demo", minutes=2)
    _closed(engine, "demo", minutes=3)
    seen_at = _seen(engine, "live", minutes=4)

    assert _live_post_mortem(engine, monkeypatch, seen_at) == [f"{BTC}: sound"]


def test_a_demo_snapshot_does_not_move_the_start_of_the_live_window(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window starts at the previous live trade's last OPEN row. A demo OPEN row between the
    live entry and that trade's close, read as the start, cut the live entry out of its window."""
    _seen(engine, "live", minutes=-10)  # live trade A, open
    _entry(engine, "live", stop="95000")  # live trade B's entry, placed while A is still open
    _seen(engine, "demo", minutes=2)  # demo on the symbol, after B's entry
    _closed(engine, "live", minutes=5)  # A closes
    seen_at = _seen(engine, "live", minutes=6)  # B open; then it closes too

    assert _live_post_mortem(engine, monkeypatch, seen_at) == [f"{BTC}: sound"]


# --- 4. the orphan re-alert window ------------------------------------------------------------


def test_a_demo_orphan_page_does_not_silence_a_live_orphan(engine: Engine, tmp_path: Path) -> None:
    demo_bot, live_bot = RecordingBot(), RecordingBot()
    _tick(engine, tmp_path, "demo", _held_btc(), demo_bot)
    assert _orphan_pages(demo_bot), "setup: the demo orphan is paged"

    _tick(engine, tmp_path, "live", _held_btc(), live_bot)

    assert _orphan_pages(live_bot), "a live orphan is real money with no plan: always paged"


def test_a_live_orphan_is_still_paged_once_a_day(engine: Engine, tmp_path: Path) -> None:
    first, second = RecordingBot(), RecordingBot()
    live = _held_btc()
    _tick(engine, tmp_path, "live", live, first)
    _tick(engine, tmp_path, "live", live, second)

    assert len(_orphan_pages(first)) == 1
    assert _orphan_pages(second) == []


# --- 5. the stale-manager alert ---------------------------------------------------------------


def test_a_demo_manager_run_is_not_a_live_one(engine: Engine) -> None:
    with start_run(engine, "manager", config_hash="demo"):
        pass

    assert alerts.last_run_at(engine, venue="demo") is not None
    assert alerts.last_run_at(engine, venue="live") is None


def test_the_live_watchdog_pages_a_stale_manager_despite_a_demo_run(
    engine: Engine, tmp_path: Path
) -> None:
    with start_run(engine, "manager", config_hash="demo"):
        pass
    bot = RecordingBot()

    _tick(engine, tmp_path, "live", FakeExchange(marks={BTC: Decimal("100000")}), bot)

    assert [t for _, t in bot.sent if "no manager run on record" in t]


def test_the_dashboard_staleness_band_reads_the_configured_account(engine: Engine) -> None:
    """The band reuses the alert's own read; it must name the same account the alert does."""
    with start_run(engine, "manager", config_hash="demo"):
        pass

    assert queries.staleness(engine, 14.0, venue="live")["stale"] is True
    assert queries.staleness(engine, 14.0, venue="demo")["stale"] is False
