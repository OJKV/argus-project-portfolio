"""Review 2026-09-18 X-8: OKX `place_bracket` idempotency only looked at *open* orders.

A market entry, or a limit that filled fast, is not in `fetch_open_orders()` (which is also
capped at 100 and not filtered by symbol). A retry after a timeout the venue had in fact
accepted therefore placed the entry a second time — twice the gated size, with a second set of
stops. The fake checked every order it had ever seen, so the fake never showed the difference.

The venue is asked by client order id instead, which covers filled and cancelled orders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt
import pytest

from argus.exchange.okx import OKXAdapter

CL = "argusabc123"


class _Venue:
    def __init__(self, held: dict[str, Any] | None) -> None:
        self.held = held
        self.created: list[dict[str, Any]] = []
        self.lookups: list[dict[str, Any]] = []

    def fetch_open_orders(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []  # a filled market order is never in the open book

    def fetch_order(self, order_id: Any, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        self.lookups.append({"id": order_id, "symbol": symbol, **params})
        if self.held is None or params.get("clOrdId") != self.held["clientOrderId"]:
            raise ccxt.OrderNotFound('okx {"code":"51603","msg":"Order does not exist"}')
        return self.held

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {"id": "venue-2", "symbol": kwargs["symbol"]}


def _adapter(monkeypatch: pytest.MonkeyPatch, venue: _Venue) -> OKXAdapter:
    instance = OKXAdapter.__new__(OKXAdapter)  # no network, no credentials
    instance.client = venue
    monkeypatch.setattr(OKXAdapter, "amount_to_precision", lambda _s, _sym, v: v)
    monkeypatch.setattr(OKXAdapter, "price_to_precision", lambda _s, _sym, v: v)
    monkeypatch.setattr(OKXAdapter, "_market", lambda _s, _sym: {"precision": {"price": 0.1}})
    return instance


def _order() -> dict[str, Any]:
    return {
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "size": Decimal("1"),
        "order_type": "market",
        "cl_ord_id": CL,
        "attach_algos": [{"kind": "sl", "trigger_px": Decimal("70000")}],
    }


def test_a_retry_after_a_filled_market_entry_does_not_place_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filled = {"id": "venue-1", "clientOrderId": CL, "status": "closed", "filled": 1.0}
    venue = _Venue(held=filled)
    ids = _adapter(monkeypatch, venue).place_bracket(_order())
    assert venue.created == [], "the entry was placed twice"
    assert ids["entry_id"] == "venue-1"
    assert ids["idempotent"] == "true"


def test_a_first_attempt_is_placed(monkeypatch: pytest.MonkeyPatch) -> None:
    venue = _Venue(held=None)
    ids = _adapter(monkeypatch, venue).place_bracket(_order())
    assert len(venue.created) == 1
    assert ids["entry_id"] == "venue-2"
    assert venue.lookups and venue.lookups[0]["clOrdId"] == CL


def test_the_lookup_says_none_when_the_venue_does_not_hold_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _adapter(monkeypatch, _Venue(held=None)).fetch_order_by_client_id("BTC/USDT:USDT", CL)
        is None
    )
