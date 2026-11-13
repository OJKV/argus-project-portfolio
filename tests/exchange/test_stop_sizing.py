"""Stops that keep covering the position as its size changes.

Review 2026-09-18 X-15: the repair SL was placed with a fixed `sz` equal to the position at that
moment. After a partial close it no longer matched, and after a full close it was an orphan.
OKX's `closeFraction: "1"` closes whatever the position is when the stop fires.

Review 2026-10-07 X-N9: TRIM (`partial_close`) reduced the position and left every sized stop
at the original size — still counted as protection, though whether OKX executes a reduce-only
stop larger than the position is not something to bet the position on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from argus.exchange.fake import FakeExchange
from argus.exchange.okx import OKXAdapter

SYMBOL = "BTC/USDT:USDT"


class _Client:
    def __init__(self) -> None:
        self.algo_bodies: list[dict[str, Any]] = []
        self.amends: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []

    def private_post_trade_order_algo(self, body: dict[str, Any]) -> dict[str, Any]:
        self.algo_bodies.append(body)
        return {"data": [{"algoId": "a1", "sCode": "0"}]}

    def private_post_trade_amend_algos(self, body: dict[str, Any]) -> dict[str, Any]:
        self.amends.append(body)
        return {"data": [{"algoId": body["algoId"], "sCode": "0"}]}

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.orders.append(kwargs)
        return {"id": "o1"}


def _adapter(monkeypatch: pytest.MonkeyPatch, algos: list[dict[str, Any]]) -> OKXAdapter:
    instance = OKXAdapter.__new__(OKXAdapter)
    instance.client = _Client()
    monkeypatch.setattr(
        OKXAdapter,
        "_require_position",
        lambda _s, _sym: {"symbol": SYMBOL, "side": "long", "size": Decimal("2")},
    )
    monkeypatch.setattr(OKXAdapter, "_market", lambda _s, _sym: {"id": "BTC-USDT-SWAP"})
    monkeypatch.setattr(OKXAdapter, "amount_to_precision", lambda _s, _sym, v: v)
    monkeypatch.setattr(OKXAdapter, "fetch_algo_orders", lambda _s: algos)
    return instance


# --- X-15 ---------------------------------------------------------------------------------


def test_a_repair_stop_closes_the_whole_position_not_a_fixed_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(monkeypatch, [])
    adapter._place_algo(
        SYMBOL, {"slTriggerPx": "70000", "slOrdPx": "-1", "slTriggerPxType": "mark"}, "conditional"
    )
    body = adapter.client.algo_bodies[-1]
    assert body["closeFraction"] == "1"
    assert "sz" not in body
    assert body["reduceOnly"] == "true"


def test_a_trailing_stop_keeps_its_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """`closeFraction` is only accepted for conditional/oco orders, not move_order_stop."""
    adapter = _adapter(monkeypatch, [])
    adapter.place_trailing(SYMBOL, Decimal("2"), None)
    body = adapter.client.algo_bodies[-1]
    assert body["sz"] == "2" and "closeFraction" not in body


# --- X-N9 ---------------------------------------------------------------------------------


def _okx_algo(
    algo_id: str, kind: str, size: str, close_fraction: str | None = None
) -> dict[str, Any]:
    return {
        "algo_id": algo_id,
        "symbol": "BTC-USDT-SWAP",
        "kind": kind,
        "size": Decimal(size),
        "close_fraction": close_fraction,
        "side": "sell",
    }


def test_a_trim_resizes_every_sized_stop_and_target_to_what_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    algos = [
        _okx_algo("sl1", "sl", "2"),
        _okx_algo("tp1", "tp", "2"),
        _okx_algo("sl2", "sl", "0", close_fraction="1"),  # already tracks the position
        _okx_algo("tp2", "tp", "0.5"),  # a split target smaller than what is left
        {**_okx_algo("x", "sl", "2"), "symbol": "ETH-USDT-SWAP"},  # another instrument
    ]
    adapter = _adapter(monkeypatch, algos)
    adapter.partial_close(SYMBOL, 50)
    assert adapter.client.orders[-1]["amount"] == 1.0
    resized = {a["algoId"]: a["newSz"] for a in adapter.client.amends}
    assert resized == {"sl1": "1", "tp1": "1"}


def test_the_fake_trim_resizes_its_stop_too() -> None:
    ex = FakeExchange()
    ids = ex.place_bracket(
        {
            "symbol": "BTC-USDT-SWAP",
            "side": "buy",
            "size": Decimal("2"),
            "price": Decimal("75000"),
            "order_type": "limit",
            "cl_ord_id": "trim0001",
            "attach_algos": [{"kind": "sl", "trigger_px": Decimal("70000")}],
        }
    )
    ex.fill(ids["entry_id"])
    ex.partial_close("BTC-USDT-SWAP", 50)
    stop = ex.algos[ids["sl_algo_id"]]
    assert stop.size == ex.positions["BTC-USDT-SWAP"].size == Decimal("1")


def test_a_trim_that_cannot_list_its_stops_still_reports_the_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 2026-10-07 (exec): the algo listing sat outside the guard.

    One failing ordType query (venue maintenance, F-140) raised out of `partial_close` after the
    reduce-only order had gone out, so the review journalled "TRIM failed" for a trim that
    happened. The stops are left as they are — oversized, still covering what is left.
    """
    adapter = _adapter(monkeypatch, [])

    def maintenance(_self: OKXAdapter) -> list[dict[str, Any]]:
        raise RuntimeError("algo-order query failed for ordType=conditional")

    monkeypatch.setattr(OKXAdapter, "fetch_algo_orders", maintenance)
    assert adapter.partial_close(SYMBOL, 50) == "o1"
    assert adapter.client.orders[-1]["amount"] == 1.0
    assert adapter.client.amends == []


def test_a_trim_below_the_lot_size_is_a_typed_refusal_not_a_venue_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-26: ccxt raises InvalidOrder for 50% of 0.01 BTC contracts, before our own
    rounds-to-zero check could ever run. No order may reach the venue."""
    import ccxt

    from argus.exchange.errors import TrimTooSmallError

    adapter = _adapter(monkeypatch, [])

    def below_precision(_self: OKXAdapter, _symbol: str, _amount: Decimal) -> Decimal:
        raise ccxt.InvalidOrder("amount must be greater than minimum amount precision of 0.01")

    monkeypatch.setattr(OKXAdapter, "amount_to_precision", below_precision)
    with pytest.raises(TrimTooSmallError, match="minimum"):
        adapter.partial_close(SYMBOL, 50)
    assert adapter.client.orders == []
