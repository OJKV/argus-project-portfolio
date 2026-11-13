"""Review 2026-10-07 (exec): which journalled entries make up the position the venue holds now.

Three findings on one question:

* A resting (OPEN, still in the book) same-side entry counted as placed and, being newest, its
  stop replaced the approved stop of the filled position. With the SL gone and the mark between
  the two stops, the watchdog market-closed a healthy position.
* `plan_stops_from_journal` ignored closes, unlike `journal_positions`: an orphan inherited the
  stop of an old, closed trade on the same (asset, side) — one beyond its liquidation price.
* `journal_positions` dropped every entry created before the asset's last CLOSED snapshot, which
  is stamped when the watchdog *notices* the close. An entry placed in that gap that filled later
  was paged as an orphan, "opened by hand?", every 24h.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeExchange, FakeOrder
from argus.execution.lifecycle import record_order, record_venue_ids, transition_order
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog, WatchdogResult, plan_stops_from_journal

BTC = "BTC-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _entry(
    engine: Engine,
    stop: str,
    *states: str,
    resting_in: FakeExchange | None = None,
    price: str = "100000",
) -> str:
    """A journalled long entry on BTC whose attached stop is `stop`, walked through `states`.

    With `resting_in`, the venue holds it in its book (no attached algos yet: a limit entry's
    stop arrives with the fill).
    """
    with start_run(engine, "manager", "demo") as run_id:
        order: dict[str, Any] = {
            "symbol": BTC,
            "side": "buy",
            "size": Decimal("1"),
            "order_type": "limit",
            "price": Decimal(price),
            "cl_ord_id": f"cl{stop}{len(states)}",
            "attach_algos": [{"kind": "sl", "trigger_px": Decimal(stop)}],
        }
        order_id = record_order(engine, run_id, order)
        if resting_in is not None:
            venue_id = f"v{stop}"
            resting_in.orders[venue_id] = FakeOrder(
                order_id=venue_id,
                cl_ord_id=order["cl_ord_id"],
                symbol=BTC,
                side="buy",
                kind="entry",
                order_type="limit",
                size=Decimal("1"),
                price=Decimal(price),
            )
            record_venue_ids(engine, order_id, {"entry_id": venue_id})
        for state in states:
            transition_order(engine, order_id, state, "test")
    return order_id


def _tick(
    ex: FakeExchange, engine: Engine, tmp_path: Path, bot: RecordingBot | None = None
) -> WatchdogResult:
    with start_run(engine, "watchdog", "demo") as run_id:
        return Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=bot or RecordingBot(), chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
        ).run_once(run_id)


def _sl(ex: FakeExchange) -> list[Decimal | None]:
    return [a.trigger_px for a in ex.algos.values() if a.kind == "sl" and a.state == "live"]


# --- a resting entry is not the position ------------------------------------------------------


def test_a_resting_same_side_entry_does_not_replace_the_filled_positions_stop(
    engine: Engine,
) -> None:
    ex = FakeExchange(marks={BTC: Decimal("98500")})
    _entry(engine, "95000", "OPEN", "FILLED")
    _entry(engine, "99000", "OPEN", resting_in=ex, price="99500")
    stops = plan_stops_from_journal(engine, ex.fetch_open_orders(), venue="demo")
    assert stops == {("btc", "long"): Decimal("95000")}


def test_the_watchdog_repairs_with_the_filled_stop_instead_of_closing(
    engine: Engine, tmp_path: Path
) -> None:
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    _entry(engine, "95000", "OPEN", "FILLED")
    _entry(engine, "99000", "OPEN", resting_in=ex, price="99500")
    ex.open_unprotected(BTC, side="long")  # entry 100k; its stop has gone missing
    ex.marks[BTC] = Decimal("98500")  # above the approved 95k, below the resting entry's 99k

    result = _tick(ex, engine, tmp_path)

    assert result.closed_past_stop == []
    assert result.repaired == [BTC]
    assert _sl(ex) == [Decimal("95000")]


# --- a closed trade's stop is not the orphan's -------------------------------------------------


def test_an_orphan_gets_the_atr_stop_not_a_closed_trades_stop(
    engine: Engine, tmp_path: Path
) -> None:
    ex = FakeExchange(marks={BTC: Decimal("60000")})
    _entry(engine, "55000", "OPEN", "FILLED", price="60000")
    ex.open_unprotected(BTC, side="long")
    ex.amend_stop(BTC, Decimal("55000"))
    _tick(ex, engine, tmp_path)  # the old trade, open
    ex.close(BTC)
    _tick(ex, engine, tmp_path)  # ...noticed closed
    assert plan_stops_from_journal(engine, ex.fetch_open_orders(), venue="demo") == {}

    ex.marks[BTC] = Decimal("100000")
    ex.leverage[BTC] = 10
    ex.open_unprotected(BTC, side="long")  # opened by hand, no SL
    result = _tick(ex, engine, tmp_path)

    assert result.repaired == [BTC]
    assert _sl(ex) == [Decimal("96000")]  # 2xATR14 from entry, inside liquidation (~90k)


def test_a_plan_stop_beyond_the_liquidation_clamp_is_not_used(
    engine: Engine, tmp_path: Path
) -> None:
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    ex.leverage[BTC] = 10
    ex.open_unprotected(BTC, side="long")
    with start_run(engine, "watchdog", "demo") as run_id:
        result = Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=RecordingBot(), chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
            plan_stops={("btc", "long"): Decimal("55000")},
        ).run_once(run_id)
    assert result.repaired == [BTC]
    (stop,) = _sl(ex)
    assert stop is not None and stop >= Decimal("95000")


# --- an entry that rested through the close is the next position -------------------------------


def test_an_entry_placed_before_the_close_was_noticed_is_not_an_orphan_when_it_fills(
    engine: Engine, tmp_path: Path
) -> None:
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    _entry(engine, "95000", "OPEN", "FILLED")
    ex.open_unprotected(BTC, side="long")
    ex.amend_stop(BTC, Decimal("95000"))
    _tick(ex, engine, tmp_path)  # open

    ex.close(BTC)  # the stop fills...
    gap = _entry(engine, "93000", "OPEN", resting_in=ex, price="97000")  # ...a new limit rests
    _tick(ex, engine, tmp_path)  # the watchdog notices the close only now

    ex.fill("v93000")  # the limit fills into a new position
    ex.amend_stop(BTC, Decimal("93000"))
    bot = RecordingBot()
    result = _tick(ex, engine, tmp_path, bot)

    assert gap
    assert not [t for _, t in bot.sent if "orphan" in t.lower()]
    assert not [d for d in result.discrepancies if d.startswith("orphan")]
