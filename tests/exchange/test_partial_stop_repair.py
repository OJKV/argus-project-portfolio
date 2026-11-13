"""Review 2026-10-07 (exec): the repair must never move another entry's stop.

Net mode: a second same-side entry adds to the position and carries its own attached SL. The
watchdog judged each SL on its own, called a fully covered position naked, and "repaired" it with
`amend_stop`, which moves the first SL it finds to the newest plan stop — widening it — and did so
again on every tick, with a Telegram message each time.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeAlgo, FakeExchange
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog, WatchdogResult

BTC = "BTC-USDT-SWAP"
PLAN = {("btc", "long"): Decimal("94000")}


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _venue(*stops: tuple[str, str]) -> FakeExchange:
    ex = FakeExchange(marks={BTC: Decimal("100000")})
    ex.open_unprotected(BTC, side="long", size="10")
    for n, (trigger, size) in enumerate(stops):
        algo_id = f"sl{n}"
        ex.algos[algo_id] = FakeAlgo(
            algo_id, BTC, "sl", Decimal(trigger), Decimal(size), side="sell"
        )
    return ex


def _tick(ex: FakeExchange, engine: Engine, tmp_path: Path) -> WatchdogResult:
    with start_run(engine, "watchdog", "demo") as run_id:
        return Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=RecordingBot(), chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
            plan_stops=PLAN,
        ).run_once(run_id)


def test_two_partial_stops_covering_the_position_are_left_alone(
    engine: Engine, tmp_path: Path
) -> None:
    ex = _venue(("97000", "5"), ("96500", "5"))
    for _ in range(2):
        result = _tick(ex, engine, tmp_path)
        assert result.repaired == []
    assert {a.algo_id: a.trigger_px for a in ex.algos.values()} == {
        "sl0": Decimal("97000"),
        "sl1": Decimal("96500"),
    }


def test_an_under_covered_position_gets_a_new_full_stop_and_keeps_the_old_one(
    engine: Engine, tmp_path: Path
) -> None:
    ex = _venue(("97000", "5"))
    result = _tick(ex, engine, tmp_path)
    assert result.repaired == [BTC]
    assert ex.algos["sl0"].trigger_px == Decimal("97000"), "another entry's stop was moved"
    added = [a for a in ex.algos.values() if a.algo_id != "sl0"]
    assert len(added) == 1
    assert added[0].kind == "sl" and added[0].close_fraction == "1"
    assert added[0].side == "sell" and added[0].trigger_px == Decimal("94000")
    # Covered now: the next tick has nothing to repair.
    assert _tick(ex, engine, tmp_path).repaired == []
