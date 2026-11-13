"""Muse P1-5: adapter read paths raise a named error, and fake/OKX key positions the same way."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from argus.exchange.fake import FakeExchange
from argus.exchange.okx import ExchangeResponseError, OKXAdapter, collect_algo_orders
from argus.exchange.reconcile import symbol_key


class _Client:
    def private_get_account_balance(self) -> dict[str, Any]:
        return {"data": []}

    def fetch_funding_rate(self, _s: str) -> Any:
        return None

    def fetch_open_interest(self, _s: str) -> Any:
        return None


def _adapter() -> OKXAdapter:
    adapter = OKXAdapter.__new__(OKXAdapter)
    adapter.client = _Client()  # type: ignore[assignment]
    return adapter


def test_an_empty_balance_payload_is_a_named_error_not_an_index_error() -> None:
    with pytest.raises(ExchangeResponseError, match="balance"):
        _adapter().fetch_balance()


def test_a_missing_funding_payload_names_the_instrument() -> None:
    with pytest.raises(ExchangeResponseError, match="BTC/USDT:USDT"):
        _adapter().fetch_funding("BTC/USDT:USDT")


def test_a_missing_open_interest_payload_names_the_instrument() -> None:
    with pytest.raises(ExchangeResponseError, match="BTC/USDT:USDT"):
        _adapter().fetch_open_interest("BTC/USDT:USDT")


def test_algo_query_failure_names_the_type_and_stays_all_or_nothing() -> None:
    def fetch(order_type: str) -> list[dict[str, Any]]:
        if order_type == "trigger":
            raise KeyError("data")
        return [{"algoId": "1"}]

    with pytest.raises(ExchangeResponseError, match="ordType=trigger"):
        collect_algo_orders(fetch, ("conditional", "trigger"))


def test_both_symbol_spellings_share_one_key() -> None:
    assert symbol_key("BTC/USDT:USDT") == symbol_key("BTC-USDT-SWAP")
    assert symbol_key("BTC-USDT-SWAP") != symbol_key("ETH-USDT-SWAP")


def test_fake_reconcile_matches_a_stop_spelled_the_other_way() -> None:
    """A position keyed `BTC-USDT-SWAP` whose stop is keyed `BTC/USDT:USDT` is protected."""
    venue = FakeExchange()
    venue.open_unprotected("BTC-USDT-SWAP")
    venue.place_stop("BTC-USDT-SWAP", Decimal("90000"))
    for algo in venue.algos.values():
        algo.symbol = "BTC/USDT:USDT"
    result = venue.reconcile()
    assert result["unprotected"] == []
    assert result["orphan_algos"] == []
