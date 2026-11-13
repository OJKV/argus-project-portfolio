"""Review 2026-10-07 X-N3 (and the rest of 2026-09-18 X-6): the repair stop must belong to the
position it protects.

`plan_stops_from_journal` read every non-rejected plan, keyed only by asset, latest row winning.
Long BTC from plan A (stop 95k, placed); a later run approves a **short** plan B (stop 110k) that
is never placed. BTC's stop disappears, the watchdog repairs with 110k, sees the long's mark of
100k "below" it, and market-closes a healthy long.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import record_order, transition_order
from argus.journal import ledger
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog, plan_stops_from_journal

BTC = "BTC-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _plan(
    engine: Engine,
    side: str,
    stop: str,
    placed: bool,
    instrument: str = BTC,
    verdict: str = "APPROVED",
) -> str:
    with start_run(engine, "manager", "demo") as run_id:
        plan_id = ledger.append(
            engine,
            "plans",
            {
                "run_id": run_id,
                "mode": "approve",
                "symbol": "BTC",
                "side": side,
                "state": "GATED",
                "payload": json.dumps({"stop": float(stop), "side": side}),
            },
        )
        ledger.append(
            engine,
            "gate_results",
            {"plan_id": plan_id, "run_id": run_id, "verdict": verdict, "reasons": "[]"},
        )
        if placed:
            order = {
                "symbol": instrument,
                "side": "buy" if side == "long" else "sell",
                "size": Decimal("1"),
                "order_type": "limit",
                "price": Decimal("100000"),
                "cl_ord_id": f"cl{plan_id[:20]}",
                "attach_algos": [{"kind": "sl", "trigger_px": Decimal(stop)}],
            }
            order_id = record_order(engine, run_id, order, plan_id=plan_id)
            transition_order(engine, order_id, "OPEN", "venue accepted")
    return plan_id


def test_a_never_placed_plan_on_the_other_side_does_not_replace_the_live_stop(
    engine: Engine,
) -> None:
    _plan(engine, "long", "95000", placed=True)
    _plan(engine, "short", "110000", placed=False)
    stops = plan_stops_from_journal(engine, [], venue="demo")
    assert stops == {("btc", "long"): Decimal("95000")}


def test_an_approved_but_unplaced_plan_contributes_no_stop(engine: Engine) -> None:
    _plan(engine, "long", "95000", placed=False)
    assert plan_stops_from_journal(engine, [], venue="demo") == {}


def test_both_symbol_spellings_find_the_same_stop(engine: Engine) -> None:
    _plan(engine, "long", "95000", placed=True, instrument="BTC/USDT:USDT")
    assert plan_stops_from_journal(engine, [], venue="demo") == {("btc", "long"): Decimal("95000")}


def _watchdog(ex: FakeExchange, engine: Engine, tmp_path: Path, stops: object) -> Watchdog:
    return Watchdog(
        exchange=ex,
        engine=engine,
        notifier=Notifier(bot=RecordingBot(), chat_id="42"),
        settings=Settings(mode="demo", argus_home=tmp_path),
        plan_stops=stops,  # type: ignore[arg-type]
    )


def test_the_review_scenario_repairs_the_long_instead_of_closing_it(
    engine: Engine, tmp_path: Path
) -> None:
    _plan(engine, "long", "95000", placed=True)
    _plan(engine, "short", "110000", placed=False)
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    ex.open_unprotected(BTC, side="long")  # entry 100k, its stop has vanished
    stops = plan_stops_from_journal(engine, ex.fetch_open_orders(), venue="demo")
    with start_run(engine, "watchdog", "demo") as run_id:
        result = _watchdog(ex, engine, tmp_path, stops).run_once(run_id)
    assert result.closed_past_stop == []
    assert result.repaired == [BTC]
    stop = next(a for a in ex.algos.values() if a.kind == "sl")
    assert stop.trigger_px == Decimal("95000")


def test_a_plan_stop_on_the_wrong_side_of_the_position_is_not_used(
    engine: Engine, tmp_path: Path
) -> None:
    """A long's stop above its entry would close it at market; fall back to the ATR stop."""
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    ex.open_unprotected(BTC, side="long")
    stops = {("btc", "long"): Decimal("105000")}
    with start_run(engine, "watchdog", "demo") as run_id:
        result = _watchdog(ex, engine, tmp_path, stops).run_once(run_id)
    assert result.closed_past_stop == []
    assert result.repaired == [BTC]
    stop = next(a for a in ex.algos.values() if a.kind == "sl")
    assert stop.trigger_px is not None and stop.trigger_px < Decimal("100000")
