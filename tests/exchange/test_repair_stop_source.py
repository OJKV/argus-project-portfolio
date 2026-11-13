"""Review 2026-09-18 X-17: the repair stop's ATR and its label.

* `atr_from_ohlcv` says "closed candles" but averaged the last row too — which on OKX is the
  bar still forming, so one spike in progress could move the emergency stop.
* The alert said "from 2xATR14" even when no ATR was available (percentage fallback) or when the
  liquidation clamp, not the ATR, set the distance. An operator reading the alert was told the
  wrong reason for the level.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog, atr_from_ohlcv, repair_stop

BTC = "BTC-USDT-SWAP"


def test_the_forming_candle_is_not_in_the_average() -> None:
    closed = [[float(i), 100.0, 100.5, 99.5, 100.0, 1.0] for i in range(15)]
    forming = [15.0, 100.0, 150.0, 50.0, 100.0, 1.0]  # a spike still in progress
    assert atr_from_ohlcv([*closed, forming]) == Decimal("1")


def test_the_label_says_which_rule_set_the_stop() -> None:
    long_pos = {"side": "long", "entry_price": Decimal("100"), "leverage": 3}
    assert repair_stop(long_pos, Decimal("91"), Decimal("2"))[1] == "plan"
    assert repair_stop(long_pos, None, Decimal("2"))[1] == "2xATR14"
    _, source = repair_stop(long_pos, None, None)
    assert "ATR" not in source.split("(")[0] and "%" in source
    _, source = repair_stop({**long_pos, "leverage": 10}, None, Decimal("8"))
    assert "liquidation" in source


def test_the_alert_does_not_claim_an_atr_it_never_had(tmp_path: Path) -> None:
    engine = open_journal(tmp_path / "journal.db")
    ex = FakeExchange()
    ex.open_unprotected(BTC)

    def no_candles(symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        raise RuntimeError("candles unavailable")

    ex.fetch_ohlcv = no_candles  # type: ignore[method-assign]
    bot = RecordingBot()
    with start_run(engine, "watchdog", "demo") as run_id:
        Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=bot, chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
        ).run_once(run_id)
    repaired = next(t for _, t in bot.sent if "Stop repaired" in t)
    assert "2xATR14" not in repaired
