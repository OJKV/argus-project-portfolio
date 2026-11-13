"""Review 2026-10-07 X-N7 (2026-09-18 X-13): attached SL/TP triggers were never tick-rounded.

`build_bracket_params` sent `str(algo["trigger_px"])` straight from the plan, and the plan's stop
is an LLM float (`Decimal(str(69999.94999999999))`). OKX either rejects the whole order for an
off-tick trigger — the entry with it — or rounds it itself, possibly *wider* than the level the
Risk Gate approved. Rounding is now done here, and a stop only ever rounds towards the entry.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt
import pytest

from argus.exchange.okx import OKXAdapter, round_attached_triggers


class _Client:
    def __init__(self) -> None:
        self.sent: dict[str, Any] = {}

    def fetch_order(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        raise ccxt.OrderNotFound("51603")

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.sent = kwargs
        return {"id": "venue-1", "symbol": kwargs["symbol"]}


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> OKXAdapter:
    instance = OKXAdapter.__new__(OKXAdapter)  # no network, no credentials
    instance.client = _Client()
    monkeypatch.setattr(OKXAdapter, "amount_to_precision", lambda _s, _sym, v: v)
    monkeypatch.setattr(OKXAdapter, "price_to_precision", lambda _s, _sym, v: v)
    monkeypatch.setattr(OKXAdapter, "_market", lambda _s, _sym: {"precision": {"price": 0.1}})
    return instance


def _order(side: str, price: str, stop: Any, target: Any) -> dict[str, Any]:
    return {
        "symbol": "BTC/USDT:USDT",
        "side": side,
        "size": Decimal("1"),
        "price": Decimal(price),
        "order_type": "limit",
        "cl_ord_id": "tick0001",
        "attach_algos": [
            {"kind": "sl", "trigger_px": Decimal(str(stop))},
            {"kind": "tp", "trigger_px": Decimal(str(target))},
        ],
    }


def _sent(adapter: OKXAdapter) -> dict[str, str]:
    algos = adapter.client.sent["params"]["attachAlgoOrds"]
    return {
        "sl": next(a["slTriggerPx"] for a in algos if "slTriggerPx" in a),
        "tp": next(a["tpTriggerPx"] for a in algos if "tpTriggerPx" in a),
    }


def test_a_long_stop_rounds_up_towards_the_entry_never_wider(adapter: OKXAdapter) -> None:
    adapter.place_bracket(_order("buy", "75000", 69999.94999999999, 80000.06))
    sent = _sent(adapter)
    assert sent["sl"] == "70000.0"  # up: tighter than the approved 69999.95, never below it
    assert sent["tp"] == "80000.0"  # towards the entry: never asks for more than planned


def test_a_short_stop_rounds_down_towards_the_entry(adapter: OKXAdapter) -> None:
    adapter.place_bracket(_order("sell", "75000", "80000.07", "70000.04"))
    sent = _sent(adapter)
    assert sent["sl"] == "80000.0"
    assert sent["tp"] == "70000.1"


def test_an_on_tick_trigger_is_unchanged(adapter: OKXAdapter) -> None:
    adapter.place_bracket(_order("buy", "75000", "70000.1", "80000.5"))
    assert _sent(adapter) == {"sl": "70000.1", "tp": "80000.5"}


def test_a_stop_that_rounds_onto_the_entry_is_refused() -> None:
    order = _order("buy", "100.0", "99.96", "110")
    with pytest.raises(ValueError, match="entry"):
        round_attached_triggers(order, Decimal("0.1"))


def test_a_missing_tick_is_an_error_not_a_guess() -> None:
    with pytest.raises(ValueError, match="tick"):
        round_attached_triggers(_order("buy", "75000", "70000", "80000"), Decimal("0"))
