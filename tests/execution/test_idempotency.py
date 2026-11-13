"""Client order ids: the same intention must always produce the same id.

A retry after a timeout is the dangerous case. The request may have reached OKX and the reply may
have been lost, so the second attempt has to be recognisable as the same order — otherwise a
network hiccup becomes two positions.
"""

from __future__ import annotations

import pytest

from argus.execution.idempotency import MAX_LEN, client_order_id


def test_the_same_intention_yields_the_same_id() -> None:
    first = client_order_id("run-abc", "BTC-USDT-SWAP", "long")
    second = client_order_id("run-abc", "BTC-USDT-SWAP", "long")
    assert first == second


def test_different_intentions_differ() -> None:
    base = client_order_id("run-abc", "BTC-USDT-SWAP", "long")
    assert base != client_order_id("run-abc", "BTC-USDT-SWAP", "short")
    assert base != client_order_id("run-abc", "ETH-USDT-SWAP", "long")
    assert base != client_order_id("run-xyz", "BTC-USDT-SWAP", "long")


@pytest.mark.parametrize(
    "symbol",
    ["BTC-USDT-SWAP", "BTC/USDT:USDT", "1000PEPE-USDT-SWAP", "a-very-long-instrument-name-here"],
)
def test_the_id_always_satisfies_okx(symbol: str) -> None:
    """OKX rejects anything over 32 characters or outside [A-Za-z0-9]."""
    value = client_order_id("run-abc", symbol, "long")
    assert 1 <= len(value) <= MAX_LEN
    assert value.isalnum()


def test_both_spellings_of_a_symbol_produce_the_same_id() -> None:
    """Positions arrive as `BTC/USDT:USDT` and instruments as `BTC-USDT-SWAP`. If those produced
    different ids, a reconcile-then-retry would not recognise its own order."""
    assert client_order_id("r", "BTC/USDT:USDT", "long") == client_order_id(
        "r", "BTC-USDT-SWAP", "long"
    )


def test_case_and_whitespace_do_not_change_the_id() -> None:
    assert client_order_id("r", " btc-usdt-swap ", "LONG") == client_order_id(
        "r", "BTC-USDT-SWAP", "long"
    )


def test_the_prefix_makes_our_orders_recognisable_on_the_exchange() -> None:
    """An operator looking at OKX should be able to tell which orders are ours."""
    assert client_order_id("run-abc", "BTC-USDT-SWAP", "long").startswith("argus")
