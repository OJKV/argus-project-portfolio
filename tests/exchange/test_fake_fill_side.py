"""Review 2026-10-07 X-N8: a filled fake position must speak the venue's `long`/`short`.

OKX reports a position's side as `long`/`short`; orders travel as `buy`/`sell`. The fake stored
the order's `buy` on the position it created at fill time, so every consumer that tests
`side == "long"` — the watchdog's breach check and its emergency stop — read a filled long as a
short. The existing watchdog tests never noticed because they open positions with
`open_unprotected(side="long")` and never go through a fill.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog

BTC = "BTC-USDT-SWAP"


def _bracket(side: str, price: str, stop: str) -> dict[str, object]:
    return {
        "symbol": BTC,
        "side": side,
        "size": Decimal("1"),
        "price": Decimal(price),
        "order_type": "limit",
        "cl_ord_id": f"fill{side}",
        "attach_algos": [{"kind": "sl", "trigger_px": Decimal(stop)}],
    }


@pytest.mark.parametrize(("side", "expected"), [("buy", "long"), ("sell", "short")])
def test_a_filled_position_reports_long_or_short(side: str, expected: str) -> None:
    ex = FakeExchange()
    stop = "70000" if side == "buy" else "80000"
    ids = ex.place_bracket(_bracket(side, "75000", stop))
    ex.fill(ids["entry_id"])
    assert ex.fetch_positions()[0]["side"] == expected


def test_watchdog_reads_a_filled_long_as_a_long(tmp_path: Path) -> None:
    """Fill a long, lose its stop, let the market fall through the repair level: close it.

    With `buy` on the position the watchdog computed a short's stop (above entry) and asked
    whether mark >= stop — the inverted breach check the review describes.
    """
    engine: Engine = open_journal(tmp_path / "journal.db")
    ex = FakeExchange()
    ids = ex.place_bracket(_bracket("buy", "75000", "70000"))
    ex.fill(ids["entry_id"])
    for algo in ex.algos.values():
        algo.state = "cancelled"  # the stop is gone; the watchdog must notice
    ex.marks[BTC] = Decimal("60000")  # far below any long's repair stop
    bot = RecordingBot()
    with start_run(engine, "watchdog", "cfg") as run_id:
        result = Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=bot, chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
        ).run_once(run_id)
    assert result.closed_past_stop == [BTC]
    assert ex.positions == {}
