"""The book the gate judges against must be the real one (review 2026-10-07 G-N1, G-15, G-3).

Until this test file the manager handed `evaluate` a `TradingHistory` of constants, `positions=[]`,
`entries_this_run` that never moved and `peak_equity_30d = equity`. Gates 6, 9, 10 and 14 were
inert on the order-placing path: two runs x two plans could put 20 % of a $150 account at risk
against a 5 % heat cap, and a -6 % week never halted anything.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

import argus.cli as cli
import argus.runs.manager as manager_mod
from argus.config import get_settings
from argus.features.universe import Asset
from argus.gate.rules import OpenPosition
from argus.gate.sizing import StepPolicy
from argus.journal import ledger
from argus.journal.db import new_id, open_journal
from argus.runs.manager import (
    gate_book,
    pending_entries,
    run_manager,
    trading_history,
    venue_positions,
    venue_stops,
)
from tests.runs.test_manager_shadow import NOW, _deps, _good_plan

ISO = NOW.isoformat()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _verdicts(engine: Engine) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.symbol, g.verdict, g.reasons FROM plans p"
                " JOIN gate_results g ON g.plan_id = p.plan_id"
                " WHERE p.run_id IN (SELECT run_id FROM runs WHERE kind = 'manager')"
            )
        ).mappings()
        return {r["symbol"]: {"verdict": r["verdict"], "reasons": r["reasons"]} for r in rows}


def _first_reject(reasons: str) -> dict[str, Any]:
    return next(r for r in json.loads(reasons)["reasons"] if r["verdict"] == "REJECT")


def _a_run(engine: Engine, stamp: str = ISO, venue: str = "demo") -> str:
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r, 'watchdog', 'demo', :t, 'OK', 0, :v)"
            ),
            {"r": run_id, "t": stamp, "v": venue},
        )
    return run_id


def _plan_row(
    engine: Engine, run_id: str, symbol: str, side: str = "long", rules: list[Any] | None = None
) -> str:
    plan_id = new_id()
    ledger.append(
        engine,
        "plans",
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "created_at": ISO,
            "mode": "demo",
            "symbol": symbol,
            "side": side,
            "state": "GATED",
            "payload": json.dumps(
                {"stop": 94.0, "market": {"beta_btc": 1.1}, "invalidation_rules": rules or []}
            ),
        },
    )
    ledger.append(
        engine,
        "gate_results",
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "created_at": ISO,
            "verdict": "APPROVED",
            "reasons": json.dumps(
                {"status": "APPROVED", "sized": {"risk_usd": 40.0, "notional_usd": 600.0}}
            ),
        },
    )
    return plan_id


def _resting_entry(
    engine: Engine,
    symbol: str,
    created_at: str = ISO,
    venue: str = "demo",
    rules: list[Any] | None = None,
    state: str = "OPEN",
) -> None:
    """An entry order driven to `state` along the legal path, appended, never edited.

    The ledger is insert-only and refuses UPDATE/DELETE (J-N1), so a test cannot rewrite history
    to reach a state; it appends the transitions that would have got there.
    """
    paths = {
        "NEW": [],
        "OPEN": [("NEW", "OPEN")],
        "PARTIAL": [("NEW", "OPEN"), ("OPEN", "PARTIAL")],
        "FILLED": [("NEW", "OPEN"), ("OPEN", "FILLED")],
        "REJECTED": [("NEW", "REJECTED")],
    }
    run_id = _a_run(engine, venue=venue)
    plan_id = _plan_row(engine, run_id, symbol, rules=rules)
    order_id = new_id()
    ledger.append(
        engine,
        "orders",
        {
            "order_id": order_id,
            "cl_ord_id": new_id()[:16],
            "run_id": run_id,
            "plan_id": plan_id,
            "created_at": created_at,
            "symbol": f"{symbol}-USDT-SWAP",
            "side": "buy",
            "kind": "entry",
            "order_type": "limit",
            "price": "99",
            "size": "6",
            "state": "NEW",
            "raw_request": "{}",
        },
    )
    for from_state, to_state in paths[state]:
        ledger.append(
            engine,
            "position_events",
            {
                "run_id": run_id,
                "created_at": created_at,
                "symbol": f"{symbol}-USDT-SWAP",
                "plan_id": plan_id,
                "machine": "order",
                "from_state": from_state,
                "to_state": to_state,
                "reason": f"-> {to_state}",
                "detail": json.dumps({"order_id": order_id}),
            },
        )


def _closed_trade(
    engine: Engine,
    symbol: str,
    pnl: str | None,
    closed_at: str,
    venue: str = "demo",
    last_unrealised: str = "",
) -> None:
    """A close as the watchdog books it: `pnl` is the venue's realised figure (None: unknown)."""
    run_id = _a_run(engine, venue=venue)
    open_id = new_id()
    closing = {"closed_from": open_id} | ({} if pnl is None else {"realised_pnl": pnl})
    with engine.begin() as conn:
        for row in (
            {
                "i": open_id,
                "state": "OPEN",
                "u": last_unrealised,
                "t": closed_at,
                "raw": "{}",
            },
            {
                "i": new_id(),
                "state": "CLOSED",
                "u": "",
                "t": closed_at,
                "raw": json.dumps(closing),
            },
        ):
            conn.execute(
                text(
                    "INSERT INTO positions_snapshots (snapshot_id, run_id, created_at, symbol,"
                    " side, size, entry_price, mark_price, leverage, margin, unrealised, state,"
                    " protection, raw) VALUES (:i, :r, :t, :s, 'long', '1', '100', '95', '2', '',"
                    " :u, :state, 'SL_ATTACHED', :raw)"
                ),
                {**row, "r": run_id, "s": f"{symbol}/USDT:USDT"},
            )


def _three_assets() -> list[Asset]:
    return [
        Asset("BTC", "BTC-USDT-SWAP", "bitcoin", "core"),
        Asset("ETH", "ETH-USDT-SWAP", "ethereum", "core"),
        Asset("SOL", "SOL-USDT-SWAP", "solana", "core"),
    ]


@pytest.mark.usefixtures("live_limits")
def test_a_third_approval_in_one_run_is_rejected(engine: Engine) -> None:
    run_manager(engine, _deps(assets=_three_assets), now=NOW, shadow=True)

    verdicts = _verdicts(engine)
    placeable = [s for s, v in verdicts.items() if v["verdict"] in {"APPROVED", "ADJUSTED"}]
    assert len(placeable) == 2
    third = next(v for s, v in verdicts.items() if s not in placeable)
    assert third["verdict"] == "REJECTED"
    assert _first_reject(third["reasons"])["gate"] == 9
    assert "entries this run" in _first_reject(third["reasons"])["detail"]


@pytest.mark.usefixtures("live_limits")
def test_an_approval_earlier_in_the_run_counts_against_heat(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-15: the second plan of a run must see the first one's risk. At step 3 each trade puts
    3.6 % at risk, so two together pass the 5 % heat cap - gate 6, not the per-run entry cap."""
    step3 = (StepPolicy(3, 5.0, 10, 5, 0.60),)
    monkeypatch.setattr(manager_mod, "risk_policy", lambda *_a: ("aggressive", step3, 4))

    run_manager(engine, _deps(), now=NOW, shadow=True)

    verdicts = _verdicts(engine)
    assert verdicts["BTC"]["verdict"] == "APPROVED"
    second = _first_reject(verdicts["SOL"]["reasons"])
    assert second["gate"] == 6 and "open risk" in second["detail"]


def test_the_same_symbol_twice_in_one_run_is_rejected_as_a_duplicate(engine: Engine) -> None:
    """`argus manager --symbols SOL,SOL` researches SOL once and gates it twice."""
    sol = Asset("SOL", "SOL-USDT-SWAP", "solana", "core")

    run_manager(engine, _deps(assets=lambda: [sol, sol]), now=NOW, shadow=True)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT verdict, reasons FROM gate_results")).all()
    assert [verdict for verdict, _ in rows] == ["APPROVED", "REJECTED"]
    assert _first_reject(rows[1][1])["gate"] == 10


def test_an_approval_earlier_in_the_run_spends_free_margin(engine: Engine) -> None:
    """Each plan needs $165 margin. $250 free covers one (x 0.9 = $225), not two."""
    run_manager(engine, _deps(portfolio=lambda: (2000.0, 250.0, [])), now=NOW, shadow=True)

    verdicts = _verdicts(engine)
    assert verdicts["BTC"]["verdict"] == "APPROVED"
    second = _first_reject(verdicts["SOL"]["reasons"])
    assert second["gate"] == 12 and "INSUFFICIENT_BALANCE" in second["detail"]


@pytest.mark.parametrize("state", ["NEW", "OPEN", "PARTIAL"])
def test_every_entry_state_that_can_still_fill_is_in_the_book(engine: Engine, state: str) -> None:
    """NEW: written before the venue call, possibly live. PARTIAL: the rest is still resting."""
    _resting_entry(engine, "SOL", state=state)

    pending = pending_entries(engine, held=set())

    assert [(p.symbol, p.risk_usd, p.notional_usd) for p in pending] == [("SOL", 40.0, 600.0)]


def test_a_position_held_on_the_venue_rejects_a_new_plan_on_that_symbol(engine: Engine) -> None:
    rows = [
        {
            "symbol": "SOL/USDT:USDT",
            "side": "long",
            "size": Decimal("5"),
            "entry_price": Decimal("100"),
            "mark_price": Decimal("101"),
        }
    ]
    held = venue_positions(engine, rows, {"SOL/USDT:USDT": Decimal("1")}, venue_stops([]))
    assert held == [OpenPosition("SOL", "long", 505.0, 505.0, None)]

    run_manager(engine, _deps(portfolio=lambda: (2000.0, 1800.0, held)), now=NOW, shadow=True)

    sol = _verdicts(engine)["SOL"]
    assert sol["verdict"] == "REJECTED"
    assert _first_reject(sol["reasons"])["gate"] == 10


def test_a_venue_position_takes_its_risk_from_the_plan_that_opened_it(engine: Engine) -> None:
    _resting_entry(engine, "SOL")  # the entry order and its plan: stop 94, beta 1.1
    rows = [
        {
            "symbol": "SOL/USDT:USDT",
            "side": "buy",  # the fake adapter's spelling after a fill (X-N8)
            "size": Decimal("5"),
            "entry_price": Decimal("100"),
            "mark_price": Decimal("101"),
        }
    ]
    [held] = venue_positions(engine, rows, {"SOL/USDT:USDT": Decimal("1")}, _sl(94))
    assert held.side == "long"
    assert held.risk_usd == pytest.approx(30.0)  # 5 contracts x |100 - 94|
    assert held.beta_btc == pytest.approx(1.1)


def _sl(*triggers: float | None) -> dict[str, list[Decimal | None]]:
    """The venue's protective algos on SOL, as `fetch_algo_orders` lists them."""
    return venue_stops(
        [
            {
                "symbol": "SOL-USDT-SWAP",
                "kind": "sl" if t is not None else "trailing",
                "trigger_px": None if t is None else Decimal(str(t)),
            }
            for t in triggers
        ]
        + [{"symbol": "SOL-USDT-SWAP", "kind": "tp", "trigger_px": Decimal("110")}]
    )


@pytest.mark.parametrize(
    ("stops", "risk"),
    [
        (_sl(97), 30.0),  # a tighter venue stop: the plan's distance still bounds the count
        (_sl(90), 50.0),  # moved away (R-1 widening): 5 x |100 - 90|, not the plan's 30
        (_sl(None), 30.0),  # a trailing stop has no fixed price: the plan's distance
        ({}, 505.0),  # naked - no stop resting at the venue: the whole notional
    ],
)
def test_open_risk_counts_the_stop_actually_resting_at_the_venue(
    engine: Engine, stops: dict[str, list[Decimal | None]], risk: float
) -> None:
    """A position whose SL algo is missing, or whose stop moved away, counted plan-stop risk."""
    _resting_entry(engine, "SOL")  # the plan: stop 94
    rows = [
        {
            "symbol": "SOL/USDT:USDT",
            "side": "long",
            "size": Decimal("5"),
            "entry_price": Decimal("100"),
            "mark_price": Decimal("101"),
        }
    ]
    [held] = venue_positions(engine, rows, {"SOL/USDT:USDT": Decimal("1")}, stops)
    assert held.risk_usd == pytest.approx(risk)


def test_a_resting_entry_from_an_earlier_run_counts_as_held(engine: Engine) -> None:
    _resting_entry(engine, "SOL")

    pending = pending_entries(engine, held=set())
    assert [(p.symbol, p.side, p.risk_usd) for p in pending] == [("SOL", "long", 40.0)]

    run_manager(engine, _deps(), now=NOW, shadow=True)
    sol = _verdicts(engine)["SOL"]
    assert sol["verdict"] == "REJECTED"
    assert _first_reject(sol["reasons"])["gate"] == 10


@pytest.mark.usefixtures("live_limits")
def test_a_partly_filled_entry_counts_its_unfilled_rest(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 6-contract SOL limit fills 1 contract. The venue reports 1/6 of the plan's risk and the
    other 5/6 still rests at OKX. Skipping every held asset left that rest out of heat, so a
    second plan was approved against risk that arrives the moment the rest fills."""
    _resting_entry(engine, "SOL", state="PARTIAL")  # the plan: risk $40, notional $600, beta 1.1
    filled = OpenPosition("SOL", "long", 101.0, 6.0, 1.1, 900.0)  # 1 contract x |100 - 94|

    from argus.runs.manager import gate_book

    assert gate_book(engine, [filled]) == [OpenPosition("SOL", "long", 600.0, 40.0, 1.1, 900.0)]

    # $40 resting + a $72 step-3 BTC trade is 5.6 % of $2000: over the 5 % cap. Counting only
    # the filled $6 it was 3.9 % and approved.
    step3 = (StepPolicy(3, 5.0, 10, 5, 0.60),)
    monkeypatch.setattr(manager_mod, "risk_policy", lambda *_a: ("aggressive", step3, 4))
    btc_only = _deps(proposer=lambda r, d, m: _good_plan(d.symbol) if d.symbol == "BTC" else None)
    btc_only.portfolio = lambda: (2000.0, 1800.0, [filled])

    run_manager(engine, btc_only, now=NOW, shadow=True)

    btc = _first_reject(_verdicts(engine)["BTC"]["reasons"])
    assert btc["gate"] == 6 and "open risk" in btc["detail"]


def test_closed_losses_today_stop_new_entries(engine: Engine) -> None:
    # -$70 on $2000 equity = -3.5 % today: past the daily limit.
    _closed_trade(engine, "ETH", "-70", (NOW - timedelta(hours=2)).isoformat())

    history = trading_history(engine, 2000.0, NOW)
    assert history.daily_pnl_pct == pytest.approx(-3.5)
    assert history.stop_outs_by_symbol == {"ETH": [NOW - timedelta(hours=2)]}

    run_manager(engine, _deps(), now=NOW, shadow=True)
    for verdict in _verdicts(engine).values():
        assert verdict["verdict"] == "REJECTED"
        assert "daily loss" in _first_reject(verdict["reasons"])["detail"]


def test_a_losing_week_counts_even_when_today_is_flat(engine: Engine) -> None:
    for days in (2, 3, 4):
        _closed_trade(engine, "ETH", "-45", (NOW - timedelta(days=days)).isoformat())

    history = trading_history(engine, 2000.0, NOW)
    assert history.daily_pnl_pct == 0.0
    assert history.weekly_pnl_pct == pytest.approx(-6.75)
    assert history.consecutive_losses == 3


def test_a_weekly_loss_writes_the_halt_it_demands(engine: Engine, tmp_path: Path) -> None:
    """G-13c: gate 9 computed `halt=True` for a weekly loss and the flag was dropped — the halt
    "lifted by itself" the moment the week rolled. The spec says HALT, which a human clears."""
    for days in (2, 3, 4):
        _closed_trade(engine, "ETH", "-50", (NOW - timedelta(days=days)).isoformat())
    halt = tmp_path / "HALT"
    armed = _deps(execute=lambda *_a: None)  # nothing is approved, so nothing is placed

    run_manager(engine, armed, now=NOW, shadow=False, halt_file=halt)

    assert halt.exists() and "weekly loss" in halt.read_text(encoding="utf-8")


def test_a_shadow_run_never_writes_the_production_halt(engine: Engine, tmp_path: Path) -> None:
    """A shadow run may gate against an invented `--equity`: a real -$70 week on a $2000 account
    reads as -70 % of $100. Writing HALT from that stopped every armed run until a human came."""
    _closed_trade(engine, "ETH", "-70", (NOW - timedelta(days=2)).isoformat())
    halt = tmp_path / "HALT"

    result = run_manager(
        engine, _deps(portfolio=lambda: (100.0, 100.0, [])), now=NOW, shadow=True, halt_file=halt
    )

    assert "weekly loss" in _first_reject(_verdicts(engine)["BTC"]["reasons"])["detail"]
    assert result.status == "OK"
    assert not halt.exists()


def test_the_reduced_size_period_survives_a_winning_trade(engine: Engine) -> None:
    """G-13b: the pause anchor is the loss that completed the streak, not the current streak."""
    third = NOW - timedelta(days=3, hours=12)
    for at in (NOW - timedelta(days=4), NOW - timedelta(days=3, hours=18), third):
        _closed_trade(engine, "ETH", "-5", at.isoformat())
    _closed_trade(engine, "ADA", "8", (NOW - timedelta(days=2)).isoformat())

    history = trading_history(engine, 2000.0, NOW)

    assert history.consecutive_losses == 0
    assert history.pause_started_at == third
    assert history.trades_since_pause == 0  # no entries since: all three reduced trades remain


def test_a_losing_streak_is_not_cut_at_a_week(engine: Engine) -> None:
    """Gate 9's "3 consecutive losses -> 24h pause" has no window. Swing trades with at most two
    positions spread three losses over more than seven days easily; loading only the week read
    losses 8 d, 6 d and 2 h ago as a streak of two, and the pause never came."""
    from argus.gate.protections import check_protections

    for at in (NOW - timedelta(days=8), NOW - timedelta(days=6), NOW - timedelta(hours=2)):
        _closed_trade(engine, "ETH", "-5", at.isoformat())

    history = trading_history(engine, 2000.0, NOW)

    assert history.consecutive_losses == 3
    assert history.pause_started_at == NOW - timedelta(hours=2)
    assert history.weekly_pnl_pct == pytest.approx(-0.5)  # the PnL sums keep their week
    assert not check_protections("BTC", history, NOW).allowed


def test_the_reduced_size_period_does_not_end_when_its_anchor_turns_a_week_old(
    engine: Engine,
) -> None:
    for days in (12, 11, 10):
        _closed_trade(engine, "ETH", "-5", (NOW - timedelta(days=days)).isoformat())
    _closed_trade(engine, "ADA", "8", (NOW - timedelta(days=9)).isoformat())

    history = trading_history(engine, 2000.0, NOW)

    assert history.pause_started_at == NOW - timedelta(days=10)
    assert history.trades_since_pause == 0  # no entries since: the x0.5 trades are all still due


def test_an_unreadable_old_close_counts_as_a_loss_in_the_streak(engine: Engine) -> None:
    """Past the week it cannot fail the run (933c00c), and it must not end a streak either."""
    _closed_trade(engine, "ETH", "garbage", (NOW - timedelta(days=10)).isoformat())
    for at in (NOW - timedelta(days=6), NOW - timedelta(hours=2)):
        _closed_trade(engine, "ETH", "-5", at.isoformat())

    assert trading_history(engine, 2000.0, NOW).consecutive_losses == 3
    assert run_manager(engine, _deps(), now=NOW, shadow=True).status == "OK"


def test_entries_already_made_today_count_against_the_daily_cap(engine: Engine) -> None:
    for symbol in ("ADA", "XRP"):
        _resting_entry(engine, symbol, created_at=(NOW - timedelta(hours=1)).isoformat())
    assert trading_history(engine, 2000.0, NOW).entries_today == 2


def test_entry_orders_the_venue_rejected_still_count_as_attempts(engine: Engine) -> None:
    """F-029 / G-18: caps count attempts. An order the venue refused is still an attempt."""
    for symbol in ("ADA", "XRP", "DOT"):
        _resting_entry(
            engine, symbol, created_at=(NOW - timedelta(hours=1)).isoformat(), state="REJECTED"
        )
    assert pending_entries(engine, held=set()) == []  # nothing is resting...
    assert trading_history(engine, 2000.0, NOW).entries_today == 3  # ...but three were tried


def test_a_closed_trade_with_no_recorded_pnl_fails_the_run(engine: Engine) -> None:
    _closed_trade(engine, "ETH", "not-a-number", (NOW - timedelta(hours=2)).isoformat())

    result = run_manager(engine, _deps(), now=NOW, shadow=True)

    assert result.status == "FAILED"
    assert result.plans == 0


def test_a_flat_close_is_zero_not_a_dead_manager(engine: Engine) -> None:
    _closed_trade(engine, "ETH", "0", (NOW - timedelta(hours=2)).isoformat())

    assert trading_history(engine, 2000.0, NOW).daily_pnl_pct == 0.0
    assert run_manager(engine, _deps(), now=NOW, shadow=True).status == "OK"


def test_a_close_is_worth_what_the_venue_realised_not_the_last_snapshot(engine: Engine) -> None:
    """Last seen at +$5 (0.1R), stopped out at -$40 before the next 20-minute tick."""
    _closed_trade(engine, "ETH", "-40", (NOW - timedelta(hours=2)).isoformat(), last_unrealised="5")

    history = trading_history(engine, 2000.0, NOW)

    assert history.daily_pnl_pct == pytest.approx(-2.0)
    assert history.consecutive_losses == 1
    assert history.stop_outs_by_symbol == {"ETH": [NOW - timedelta(hours=2)]}


def test_an_unknown_realised_pnl_is_the_loss_at_the_plan_stop(engine: Engine) -> None:
    """When the venue cannot say, the close is booked at its approved loss ($40 at the stop),
    never at the stale unrealised that read it as a win."""
    _resting_entry(engine, "ETH", created_at=(NOW - timedelta(days=1)).isoformat())
    _closed_trade(engine, "ETH", None, (NOW - timedelta(hours=2)).isoformat(), last_unrealised="5")

    history = trading_history(engine, 2000.0, NOW)

    assert history.daily_pnl_pct == pytest.approx(-2.0)
    assert history.consecutive_losses == 1


def test_an_old_bad_close_does_not_block_the_manager_for_ever(engine: Engine) -> None:
    """Only the closes the protections window reads can fail a run."""
    _closed_trade(engine, "ETH", "garbage", (NOW - timedelta(days=30)).isoformat())
    assert run_manager(engine, _deps(), now=NOW, shadow=True).status == "OK"


def test_peak_equity_comes_from_the_equity_history(engine: Engine) -> None:
    from argus.runs.manager import peak_equity_30d

    run_id = _a_run(engine)
    with engine.begin() as conn:
        for days, equity in ((40, "5000"), (10, "2600"), (1, "2100")):
            conn.execute(
                text(
                    "INSERT INTO equity_snapshots (snapshot_id, run_id, created_at, equity)"
                    " VALUES (:i, :r, :t, :e)"
                ),
                {
                    "i": new_id(),
                    "r": run_id,
                    "t": (NOW - timedelta(days=days)).isoformat(),
                    "e": equity,
                },
            )
    assert peak_equity_30d(engine, 2000.0, NOW) == 2600.0
    assert peak_equity_30d(engine, 2700.0, NOW) == 2700.0


def test_equity_override_is_refused_on_an_armed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--equity` is a shadow-only convenience; armed it would gate against an invented book."""
    from types import SimpleNamespace

    armed = SimpleNamespace(mode="approve", venue="demo")
    monkeypatch.setattr(cli, "get_settings", lambda: armed)

    def must_not_open(*_a: object, **_k: object) -> None:
        raise AssertionError("an armed --equity run got as far as opening the journal")

    monkeypatch.setattr("argus.journal.db.open_journal", must_not_open)
    result = CliRunner().invoke(cli.app, ["manager", "--llm", "--equity", "150"])
    assert result.exit_code == 2, result.output
    assert "--equity" in result.output


def test_good_plan_fixture_is_placeable_alone(engine: Engine) -> None:
    """Guard for the tests above: a single plan on an empty book is approved."""
    run_manager(
        engine,
        _deps(proposer=lambda r, d, m: _good_plan(d.symbol) if d.symbol == "BTC" else None),
        now=NOW,
        shadow=True,
    )
    assert _verdicts(engine)["BTC"]["verdict"] in {"APPROVED", "ADJUSTED"}


def test_the_risk_debate_reads_the_same_book_the_gate_judges() -> None:
    """D-N2 / G-N7: `cli.book()` sent `risk_pct` from `OpenPosition`, which has no such field,
    and `risk_flags` read `equity`, `heat_pct`, `max_heat_pct`, `recent_stop_outs` and per-position
    `beta_btc` — none of which the dict had. Every same-side position read as correlated, the
    heat flags could never fire and recent stop-outs were invisible."""
    from argus.features.risk_flags import compute_risk_flags
    from argus.runs.manager import risk_book

    positions = [
        OpenPosition("ETH", "long", 600.0, 45.0, 1.5, 900.0),
        OpenPosition("BTC", "long", 800.0, 50.0, 1.0, 900.0),
    ]
    closed = [(NOW - timedelta(hours=30), "SOL", -12.0), (NOW - timedelta(hours=2), "ADA", 9.0)]
    book = risk_book(2000.0, positions, closed, NOW)

    assert book["open_risk_pct"] == pytest.approx(4.75)
    assert [p["risk_pct"] for p in book["open_positions"]] == [2.25, 2.5]
    assert book["recent_stop_outs"] == [{"symbol": "SOL", "hours_ago": 30.0}]

    plan = {"symbol": "SOL", "side": "long", "time_stop_hours": 120, "sized": {"risk_usd": 20.0}}
    flags = compute_risk_flags(plan, book)
    assert flags["correlated_open_positions"] == ["ETH"]  # beta 1.0 is not high beta
    assert flags["book_nearly_full"] is True
    assert flags["would_exceed_heat_budget"] is True
    assert flags["recent_stop_outs_on_symbol"] == 1


def test_downside_misses_come_from_the_post_mortems(engine: Engine) -> None:
    from argus.runs.manager import downside_miss_rate

    assert downside_miss_rate(engine) is None
    run_id = _a_run(engine)
    plans = {r: _plan_row(engine, run_id, "BTC") for r in (-1.0, -1.6, -0.9, 2.0)}
    with engine.begin() as conn:
        for r, plan_id in plans.items():
            conn.execute(
                text(
                    "INSERT INTO postmortems (plan_id, created_at, run_id, symbol, outcome_r,"
                    " process_verdict, would_take_again, entry_timing, sizing, stop_placement,"
                    " report) VALUES (:p, :t, :r, 'BTC', :o, 'sound', 1, 'ok', 'ok', 'ok', '{}')"
                ),
                {"p": plan_id, "t": ISO, "r": run_id, "o": r},
            )
    # One of three losers slipped past 1.3R; the winner is not a loser.
    assert downside_miss_rate(engine) == pytest.approx(1 / 3)


def _equity(engine: Engine, venue: str, days: float, equity: str) -> None:
    run_id = _a_run(engine, venue=venue)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO equity_snapshots (snapshot_id, run_id, created_at, equity)"
                " VALUES (:i, :r, :t, :e)"
            ),
            {
                "i": new_id(),
                "r": run_id,
                "t": (NOW - timedelta(days=days)).isoformat(),
                "e": equity,
            },
        )


def test_the_live_book_ignores_the_demo_accounts_rows(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Demo and live share one journal (ARGUS_HOME). Moving from demo to live within a week let
    demo wins net against live losses, counted demo resting orders as live heat, and fed demo
    equity into the live risk profile. Rows of an unrecorded venue ('-', before F-131) are not
    this account's either."""
    from argus.runs.manager import downside_miss_rate, lowest_equity_since, peak_equity_30d

    monkeypatch.setenv("MODE", "approve")
    monkeypatch.setenv("LIVE_TRADING", "I_UNDERSTAND")
    monkeypatch.setenv("OKX_DEMO", "false")
    monkeypatch.setenv("LIVE_VENUE", "I_UNDERSTAND_REAL_MONEY")  # P-N5 interlock
    get_settings.cache_clear()
    assert get_settings().venue == "live"

    _closed_trade(engine, "ETH", "-70", (NOW - timedelta(hours=2)).isoformat(), venue="live")
    _closed_trade(engine, "BTC", "100", (NOW - timedelta(hours=3)).isoformat(), venue="demo")
    _closed_trade(engine, "SOL", "50", (NOW - timedelta(hours=4)).isoformat(), venue="-")
    _resting_entry(engine, "ADA", created_at=(NOW - timedelta(hours=1)).isoformat())
    _equity(engine, "demo", 9, "5000")
    _equity(engine, "demo", 2, "90")
    _equity(engine, "live", 8, "160")
    run_id = _a_run(engine, venue="demo")
    plan_id = _plan_row(engine, run_id, "BTC")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO postmortems (plan_id, created_at, run_id, symbol, outcome_r,"
                " process_verdict, would_take_again, entry_timing, sizing, stop_placement,"
                " report) VALUES (:p, :t, :r, 'BTC', -2.0, 'sound', 1, 'ok', 'ok', 'ok', '{}')"
            ),
            {"p": plan_id, "t": ISO, "r": run_id},
        )

    history = trading_history(engine, 2000.0, NOW)
    assert history.daily_pnl_pct == pytest.approx(-3.5)  # the demo win does not net it away
    assert history.entries_today == 0  # the demo entry was not an attempt on this account
    assert gate_book(engine, []) == []
    assert peak_equity_30d(engine, 150.0, NOW) == 160.0  # its own high, not the demo $5000
    assert lowest_equity_since(engine, NOW - timedelta(days=7), NOW, 150.0) == 150.0
    assert downside_miss_rate(engine) is None


# --- kill-switches and the portfolio switches at gate 11 (review G-3) -------------------------


def _held_sol(engine: Engine, rules: list[Any]) -> OpenPosition:
    """A filled SOL long whose plan carries `rules` (its entry is no longer resting)."""
    _resting_entry(engine, "SOL", rules=rules, state="FILLED")
    return OpenPosition("SOL", "long", 505.0, 30.0, 1.1, 900.0)


def _btc_only(**over: Any) -> Any:
    return _deps(
        proposer=lambda r, d, m: _good_plan(d.symbol) if d.symbol == "BTC" else None, **over
    )


@pytest.mark.parametrize(("sol_close", "fights"), [(94.0, True), (96.0, False)])
def test_an_open_positions_fired_invalidation_blocks_a_plan_that_fights_it(
    engine: Engine, sol_close: float, fights: bool
) -> None:
    """Gate 11 was inert: `active_kill_switches=[]` was a constant. An open long whose own rule
    "1d close below 95" has fired says longs are wrong here, and another long must not open."""
    held = _held_sol(engine, [{"kind": "price_close_below", "level": 95, "timeframe": "1d"}])
    asked: list[tuple[str, str]] = []

    def last_close(asset: str, timeframe: str) -> float:
        asked.append((asset, timeframe))
        return sol_close

    deps = _btc_only(portfolio=lambda: (2000.0, 1800.0, [held]), last_close=last_close)
    run_manager(engine, deps, now=NOW, shadow=True)

    btc = _verdicts(engine)["BTC"]
    assert asked == [("SOL", "1d")]
    if fights:
        assert _first_reject(btc["reasons"])["gate"] == 11
        assert "active kill-switch" in _first_reject(btc["reasons"])["detail"]
    else:
        assert btc["verdict"] in {"APPROVED", "ADJUSTED"}


def test_a_held_rule_with_no_closed_candle_to_judge_it_fails_the_run(engine: Engine) -> None:
    held = _held_sol(engine, [{"kind": "price_close_below", "level": 95, "timeframe": "4h"}])
    deps = _btc_only(portfolio=lambda: (2000.0, 1800.0, [held]), last_close=lambda a, t: None)

    result = run_manager(engine, deps, now=NOW, shadow=True)

    assert result.status == "FAILED" and result.plans == 0


def test_a_thin_margin_ratio_on_the_venue_stops_new_entries(engine: Engine) -> None:
    rows = [
        {
            "symbol": "SOL/USDT:USDT",
            "side": "long",
            "size": Decimal("5"),
            "entry_price": Decimal("100"),
            "mark_price": Decimal("101"),
            "margin_ratio": Decimal("150"),
        }
    ]
    held = venue_positions(engine, rows, {"SOL/USDT:USDT": Decimal("1")}, venue_stops([]))
    assert held[0].margin_ratio == 150.0

    run_manager(engine, _btc_only(portfolio=lambda: (2000.0, 1800.0, held)), now=NOW, shadow=True)

    btc = _first_reject(_verdicts(engine)["BTC"]["reasons"])
    assert btc["gate"] == 11 and "margin ratio" in btc["detail"]


def test_one_position_without_a_margin_ratio_blocks_entries_even_if_another_reports(
    engine: Engine,
) -> None:
    """min() ignored the None: a reported 900 % hid a position whose ratio the venue omitted."""
    rows = [
        {
            "symbol": sym,
            "side": "long",
            "size": Decimal("5"),
            "entry_price": Decimal("100"),
            "mark_price": Decimal("101"),
            "margin_ratio": ratio,
        }
        for sym, ratio in (("SOL/USDT:USDT", Decimal("900")), ("ETH/USDT:USDT", None))
    ]
    stops = {"SOL/USDT:USDT": Decimal("1"), "ETH/USDT:USDT": Decimal("1")}
    held = venue_positions(engine, rows, stops, venue_stops([]))

    run_manager(engine, _btc_only(portfolio=lambda: (2000.0, 1800.0, held)), now=NOW, shadow=True)

    btc = _first_reject(_verdicts(engine)["BTC"]["reasons"])
    assert btc["gate"] == 11 and "margin ratio unknown" in btc["detail"]


def test_a_drawdown_from_the_recorded_peak_stops_new_entries(engine: Engine) -> None:
    """`peak_equity_30d` was read from the journal and then never used by the gate."""
    _equity(engine, "demo", 10, "2600")  # $2000 now is 23 % below it

    run_manager(engine, _btc_only(), now=NOW, shadow=True)

    btc = _first_reject(_verdicts(engine)["BTC"]["reasons"])
    assert btc["gate"] == 11 and "drawdown" in btc["detail"]
