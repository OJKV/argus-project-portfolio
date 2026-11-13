"""What the OKX adapter actually sends, with the network replaced and nothing else.

`tests/integration/` covers manager -> executor -> venue against the fake. That seam is real, but
it is not the one F-137 broke: the fake has its own `place_bracket`, so the OKX adapter's own
translation of the order side was never executed by any test. A long was turned into `buy` by
`build_entry_order` and then back into `sell` here, and only the live venue disagreed.

This stubs ccxt and asserts on the arguments the adapter passes. It is the cheapest possible
version of "what would OKX have received".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import ccxt
import pytest

from argus.exchange.okx import OKXAdapter


class _StubClient:
    """Just enough ccxt to let `place_bracket` run, recording what it was asked to send."""

    def __init__(self) -> None:
        self.sent: dict[str, Any] = {}

    def fetch_open_orders(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    def fetch_order(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        # The idempotency lookup by client id (review X-8): this venue holds nothing yet.
        raise ccxt.OrderNotFound("51603")

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.sent = kwargs
        return {"id": "venue-1", "symbol": kwargs["symbol"]}


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> OKXAdapter:
    instance = OKXAdapter.__new__(OKXAdapter)  # no network, no credentials
    instance.client = _StubClient()  # type: ignore[assignment]
    monkeypatch.setattr(OKXAdapter, "fetch_open_orders", lambda _self: [])
    monkeypatch.setattr(OKXAdapter, "amount_to_precision", lambda _s, _sym, v: v)
    monkeypatch.setattr(OKXAdapter, "price_to_precision", lambda _s, _sym, v: v)
    # Attached triggers are tick-rounded before sending (review X-N7).
    monkeypatch.setattr(OKXAdapter, "_market", lambda _s, _sym: {"precision": {"price": 0.1}})
    return instance


def _order(side: str, stop: str) -> dict[str, Any]:
    return {
        "symbol": "BTC/USDT:USDT",
        "side": side,
        "size": Decimal("1"),
        "price": Decimal("75000"),
        "order_type": "limit",
        "cl_ord_id": "run00001plan0001",
        "attach_algos": [{"kind": "sl", "trigger_px": Decimal(stop)}],
        "reduce_only": False,
    }


def test_a_long_is_sent_to_okx_as_a_buy(adapter: OKXAdapter) -> None:
    """F-137. `build_entry_order` emits `buy`; this used to ask `== "long"` and send `sell`.

    Every long would have opened a short. The venue only objected indirectly, about the stop
    being on the wrong side, and no unit test executed this line at all.
    """
    adapter.place_bracket(_order("buy", "72000"))
    assert adapter.client.sent["side"] == "buy"  # type: ignore[attr-defined]


def test_the_plan_vocabulary_also_arrives_correctly(adapter: OKXAdapter) -> None:
    """Both vocabularies reach this function in practice, so both are pinned."""
    adapter.place_bracket(_order("long", "72000"))
    assert adapter.client.sent["side"] == "buy"  # type: ignore[attr-defined]


@pytest.mark.parametrize("side", ["sell", "short"])
def test_a_short_is_sent_as_a_sell(adapter: OKXAdapter, side: str) -> None:
    adapter.place_bracket(_order(side, "78000"))
    assert adapter.client.sent["side"] == "sell"  # type: ignore[attr-defined]


def test_the_stop_travels_in_the_same_request(adapter: OKXAdapter) -> None:
    """Rule 4 at the wire level: OKX must never see an entry without its protection."""
    adapter.place_bracket(_order("buy", "72000"))
    params = adapter.client.sent["params"]  # type: ignore[attr-defined]
    attached = params["attachAlgoOrds"]
    assert attached[0]["slTriggerPx"] == "72000.0"  # on the 0.1 tick (review X-N7)
    # Mark price, not last: a wick on the last price hunts stops that mark price ignores.
    assert attached[0]["slTriggerPxType"] == "mark"


def test_an_unrecognised_side_never_reaches_the_venue(adapter: OKXAdapter) -> None:
    with pytest.raises(ValueError, match="unrecognised side"):
        adapter.place_bracket(_order("up", "72000"))
    assert adapter.client.sent == {}  # type: ignore[attr-defined]
