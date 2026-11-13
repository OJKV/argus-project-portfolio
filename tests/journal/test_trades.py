"""A closed trade is read back from what the runs actually write (F-152).

`closed_trades` used to select on a `position_events` row (`machine='position'`, `to_state=
'closed'`) that nothing in `argus/` writes, so the Reflector's window was always empty. These tests
therefore seed the journal the way production does — a manager run places the entry, the watchdog
records the position open and later writes the CLOSED snapshot, a review stores the post-mortem —
and never insert a close row by hand. Seeding by hand is how the bug survived its own tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

from argus.execution.lifecycle import record_order, transition_order
from argus.journal import ledger
from argus.journal import positions as positions_journal
from argus.journal.db import open_journal, start_run
from argus.journal.trades import closed_trades, store_postmortem
from argus.llm.schemas import PostMortem


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _report(verdict: str = "sound") -> PostMortem:
    return PostMortem(
        process_verdict=verdict,
        would_take_again=verdict == "sound",
        entry_timing="good",
        sizing="right",
        stop_placement="right",
        missed_signals=[],
        do_differently="" if verdict == "sound" else "Place the stop beyond the level.",
        feedback="The thesis held; price reached the stop first.",
    )


def _instrument(asset: str) -> str:
    return f"{asset}-USDT-SWAP"


def _placed(engine: Engine, venue: str, asset: str, thesis: str = "EMA reclaim") -> str:
    """A long entry `venue`'s manager run placed and the venue accepted. Returns the plan id."""
    with start_run(engine, "manager", config_hash=venue) as run_id:
        plan_id = ledger.append(
            engine,
            "plans",
            {
                "run_id": run_id,
                "mode": "approve",
                "symbol": asset,
                "side": "long",
                "state": "GATED",
                "payload": json.dumps(
                    {
                        "symbol": asset,
                        "side": "long",
                        "stop": 90.0,
                        "thesis": thesis,
                        "market": {"regime": "TREND_UP"},
                    }
                ),
            },
        )
        ledger.append(
            engine,
            "gate_results",
            {"plan_id": plan_id, "run_id": run_id, "verdict": "APPROVED", "reasons": "[]"},
        )
        order = {
            "symbol": _instrument(asset),
            "side": "buy",
            "size": "1",
            "order_type": "limit",
            "price": "100",
            "cl_ord_id": f"cl{plan_id[:20]}",
        }
        transition_order(
            engine, record_order(engine, run_id, order, plan_id=plan_id), "OPEN", "accepted"
        )
    return plan_id


def _seen_open(engine: Engine, venue: str, asset: str, at: datetime) -> None:
    """A watchdog tick that finds the `asset` long still open at `at`."""
    position = {
        "symbol": _instrument(asset),
        "side": "long",
        "size": "1",
        "entry_price": 100,
        "mark_price": 95,
    }
    with start_run(engine, "watchdog", config_hash=venue) as run_id:
        positions_journal.record(engine, run_id, [position], now=at)


def _trade(engine: Engine, venue: str, asset: str, *, close: bool = True, minute: int = 0) -> str:
    """One trade through the real writers: entry placed, position seen open, then (unless
    `close` is False) gone from the venue and marked CLOSED. Returns the plan id.

    `minute` spaces trades in time; the snapshots follow the entry the way a watchdog tick follows
    a manager run.
    """
    plan_id = _placed(engine, venue, asset)
    opened = datetime.now(UTC) + timedelta(minutes=minute + 1)
    _seen_open(engine, venue, asset, opened)
    if close:
        _mark_closed(engine, venue, _instrument(asset), opened + timedelta(minutes=1))
    return plan_id


def _mark_closed(engine: Engine, venue: str, instrument: str, at: datetime) -> None:
    """A watchdog tick that no longer sees `instrument`, exactly as `Watchdog.run_once` does."""
    with start_run(engine, "watchdog", config_hash=venue) as run_id:
        gone = positions_journal.newly_closed(engine, [], venue=venue)
        positions_journal.mark_closed(
            engine, run_id, [c for c in gone if c.symbol == instrument], now=at
        )


def _judged(engine: Engine, venue: str, plan_id: str, asset: str) -> None:
    """The review's post-mortem for `plan_id`, stored the way `write_postmortems` stores it."""
    with start_run(engine, "review", config_hash=venue) as run_id:
        store_postmortem(
            engine,
            run_id=run_id,
            plan_id=plan_id,
            symbol=asset,
            report=_report("flawed"),
            outcome={"outcome_r": -1.0, "exit_reason": "left the venue between ticks"},
            flags={"entry_slippage_pct": 4.31, "stop_distance_atr": 0.4},
        )


def test_a_closed_trade_reaches_the_reflector_with_its_postmortem(engine: Engine) -> None:
    """The regression: placed plan, watchdog OPEN then CLOSED snapshot, stored post-mortem."""
    plan_id = _trade(engine, "demo", "SOL")
    _judged(engine, "demo", plan_id, "SOL")

    trades = closed_trades(engine, venue="demo")

    assert [t["trade_id"] for t in trades] == [plan_id[:8]]
    trade = trades[0]
    assert trade["symbol"] == "SOL"
    assert trade["side"] == "long"
    assert trade["thesis"] == "EMA reclaim"
    assert trade["regime_at_entry"] == "TREND_UP"
    assert trade["closed_at"], "the close time is the CLOSED snapshot's"
    assert trade["outcome_r"] == -1.0
    assert trade["exit_reason"] == "left the venue between ticks"
    assert trade["postmortem"]["process_verdict"] == "flawed"
    assert trade["postmortem"]["would_take_again"] is False
    # The flags are what the verdict was formed against (F-111) — kept so a later reader can
    # check the judgement against the numbers without re-deriving them.
    assert trade["entry_slippage_pct"] == 4.31
    assert trade["stop_distance_atr"] == 0.4


def test_the_other_accounts_trade_is_not_returned(engine: Engine) -> None:
    """One journal holds both accounts (F-149, F-150): the Reflector on one must not learn from the
    other, or a demo season becomes a lesson that moves live prompts. Same asset on both, the case
    the plan lookup is easiest to confuse."""
    demo = _trade(engine, "demo", "BTC")
    _judged(engine, "demo", demo, "BTC")
    live = _trade(engine, "live", "BTC")
    _judged(engine, "live", live, "BTC")

    assert [t["trade_id"] for t in closed_trades(engine, venue="live")] == [live[:8]]
    assert [t["trade_id"] for t in closed_trades(engine, venue="demo")] == [demo[:8]]


def test_an_account_with_no_closed_trades_of_its_own_gets_none(engine: Engine) -> None:
    _trade(engine, "demo", "SOL")
    assert closed_trades(engine, venue="live") == []


def test_only_closed_positions_are_reflected_over(engine: Engine) -> None:
    _trade(engine, "demo", "SOL", close=True)
    _trade(engine, "demo", "ETH", close=False)
    trades = closed_trades(engine, venue="demo")
    # An open position has no outcome to learn from, and including it would invite the Reflector
    # to draw a conclusion from a trade that is still running.
    assert [t["symbol"] for t in trades] == ["SOL"]


def test_a_trade_without_a_postmortem_is_still_included(engine: Engine) -> None:
    """Dropping it would silently narrow the window the Reflector believes it is reading. The
    verdict is optional work that may still be owed (F-151); the close is not."""
    _trade(engine, "demo", "ARB")
    trade = closed_trades(engine, venue="demo")[0]
    assert trade["symbol"] == "ARB"
    assert trade["outcome_r"] is None
    assert "postmortem" not in trade


def test_a_position_this_system_did_not_open_is_not_a_trade(engine: Engine) -> None:
    """A close with no placed plan behind it (an orphan the operator opened by hand) has no thesis
    and no process to judge, and it is never given a post-mortem either."""
    with start_run(engine, "watchdog", config_hash="demo") as run_id:
        positions_journal.record(
            engine,
            run_id,
            [{"symbol": _instrument("DOGE"), "side": "long", "size": "1", "entry_price": 1}],
            now=datetime.now(UTC) + timedelta(minutes=1),
        )
    _mark_closed(engine, "demo", _instrument("DOGE"), datetime.now(UTC) + timedelta(minutes=2))

    assert closed_trades(engine, venue="demo") == []


def test_a_close_marked_by_two_runs_is_one_trade(engine: Engine) -> None:
    """The watchdog and a review can both notice the same close before either marks it, leaving two
    CLOSED rows for one position. The trade is still one trade (F-151)."""
    plan_id = _placed(engine, "demo", "SOL")
    opened = datetime.now(UTC) + timedelta(minutes=1)
    _seen_open(engine, "demo", "SOL", opened)
    with start_run(engine, "watchdog", config_hash="demo") as run_id:
        noticed = positions_journal.newly_closed(engine, [], venue="demo")
        positions_journal.mark_closed(engine, run_id, noticed, now=opened + timedelta(minutes=1))
    with start_run(engine, "review", config_hash="demo") as run_id:
        positions_journal.mark_closed(engine, run_id, noticed, now=opened + timedelta(minutes=2))

    with engine.connect() as conn:
        marked = conn.execute(
            text("SELECT COUNT(*) FROM positions_snapshots WHERE state = 'CLOSED'")
        ).scalar()
    assert marked == 2, "setup: the close is recorded twice"
    assert [t["trade_id"] for t in closed_trades(engine, venue="demo")] == [plan_id[:8]]


def test_trades_arrive_oldest_first_within_the_window(engine: Engine) -> None:
    for i, asset in enumerate(["SOL", "ETH", "ARB", "OP"]):
        _trade(engine, "demo", asset, minute=i * 10)
    trades = closed_trades(engine, limit=3, venue="demo")
    # Newest three, but read oldest-first: a reflection reads a sequence, not a set.
    assert [t["symbol"] for t in trades] == ["ETH", "ARB", "OP"]
    assert closed_trades(engine, limit=0, venue="demo") == []


def test_trade_ids_are_short_and_stable(engine: Engine) -> None:
    plan_id = _trade(engine, "demo", "SOL")
    trade = closed_trades(engine, venue="demo")[0]
    assert trade["trade_id"] == plan_id[:8]
    # Short because the model has to copy them back exactly, and a full uuid invites a typo that
    # `ground()` would then read as a fabricated citation.
    assert len(trade["trade_id"]) == 8


def test_reading_closed_trades_writes_nothing(engine: Engine) -> None:
    plan_id = _trade(engine, "demo", "SOL")
    _judged(engine, "demo", plan_id, "SOL")
    tables = ("plans", "orders", "positions_snapshots", "postmortems", "position_events", "runs")

    def counts() -> list[int]:
        with engine.connect() as conn:
            return [
                int(conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0) for t in tables
            ]

    before = counts()
    closed_trades(engine, venue="demo")
    assert counts() == before


# --- the Reflector's two callers --------------------------------------------------------------


def test_the_reflection_counts_only_its_own_accounts_trades(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two live trades and five demo ones: a live reflection has two, and declines to ask (a
    lesson needs three supporters). Counting the demo season would have made seven."""
    import argus.runs.reflect as reflect_module

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("two supporters is below the floor: no call should be spent")

    monkeypatch.setattr(reflect_module, "ask_reflector", refuse)
    for asset in ["SOL", "ETH"]:
        _trade(engine, "live", asset)
    for asset in ["ARB", "OP", "APT", "SUI", "INJ"]:
        _trade(engine, "demo", asset)

    result = reflect_module.run_reflection(engine, object(), "r1", venue="live")  # type: ignore[arg-type]

    assert result.trades == 2


def test_the_reflect_dry_run_lists_this_accounts_closed_trades() -> None:
    """`argus reflect --dry-run` against the default journal and the default (demo) account."""
    from argus.cli import app

    journal = open_journal()  # the file the CLI will open: ARGUS_HOME is isolated per test
    plan_id = _trade(journal, "demo", "SOL")
    _judged(journal, "demo", plan_id, "SOL")
    _trade(journal, "live", "ETH")

    result = CliRunner().invoke(app, ["reflect", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "1 closed trades in the window" in result.output
    assert plan_id[:8] in result.output
    assert "SOL" in result.output
    assert "ETH" not in result.output
