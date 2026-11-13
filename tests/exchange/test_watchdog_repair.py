"""Phase 1 gate on the real venue: an unprotected OKX demo position is repaired by the watchdog.

CLAUDE.md rule 4: any change to `place_bracket`, `amend_stop` or `reconcile` must keep this test
green on the OKX demo environment. It deliberately opens a minimum-size BTC position *without* a
stop (bypassing `place_bracket`, which refuses to), runs one watchdog pass, and asserts a live
stop-loss exists afterwards. The position is always closed in `finally`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from argus.config import get_settings
from argus.exchange.okx import OKXAdapter
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog
from tests.exchange.demo_access import reach_demo

pytestmark = pytest.mark.demo

SYMBOL = "BTC/USDT:USDT"


@pytest.fixture(scope="module")
def adapter() -> OKXAdapter:
    settings = get_settings()
    if not settings.okx_demo:
        pytest.skip("refusing to open an unprotected position outside the demo environment")
    if not settings.okx_credentials_set:
        pytest.skip("OKX demo credentials are not set")
    client = OKXAdapter(settings)
    # Skips only when the environment is the problem; a broken adapter fails (review X-16).
    balance = reach_demo(client.fetch_balance)
    if balance["free"] <= 0:
        pytest.skip("demo account has no free USDT")
    if client.fetch_positions():
        pytest.skip("demo account is not flat — refusing to mix test and existing positions")
    return client


def test_watchdog_attaches_a_stop_to_a_naked_position(adapter: OKXAdapter, tmp_path: Path) -> None:
    size = float(adapter.instrument_spec(SYMBOL)["min_size"])
    adapter.set_leverage(SYMBOL, 3)
    adapter.client.create_order(
        SYMBOL, "market", "buy", size, params={"tdMode": "isolated", "clOrdId": "argusnaked000001"}
    )
    try:
        assert SYMBOL in adapter.reconcile()["unprotected"], "setup failed: position has a stop"

        bot = RecordingBot()
        engine = open_journal(tmp_path / "journal.db")
        with start_run(engine, "watchdog", "demo-gate") as run_id:
            result = Watchdog(
                exchange=adapter, engine=engine, notifier=Notifier(bot=bot, chat_id="test")
            ).run_once(run_id)

        assert result.repaired == [SYMBOL], result.notes
        assert result.repair_failures == []
        assert SYMBOL not in adapter.reconcile()["unprotected"]
        stops = [a for a in adapter.fetch_algo_orders() if a["kind"] == "sl"]
        assert stops, "no live stop-loss after the repair"
        position = next(p for p in adapter.fetch_positions() if p["symbol"] == SYMBOL)
        stop_px = stops[0]["trigger_px"]
        assert stop_px is not None and Decimal("0") < stop_px < position["entry_price"]
        assert any("Stop repaired" in text for _, text in bot.sent)
    finally:
        adapter.close(SYMBOL)
        for algo in adapter.fetch_algo_orders():
            adapter.client.private_post_trade_cancel_algos(
                [{"algoId": algo["algo_id"], "instId": algo["symbol"]}]
            )
    assert SYMBOL not in {p["symbol"] for p in adapter.fetch_positions()}
