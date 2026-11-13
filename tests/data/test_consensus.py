"""Price consensus — the F-010 fix (aggregators disagreed on BTC's price and direction)."""

from __future__ import annotations

from decimal import Decimal

from argus.data.consensus import consensus_price


def test_agreeing_sources_produce_a_venue_first_price() -> None:
    result = consensus_price(
        "BTC",
        {
            "coingecko.coin": Decimal("75020"),
            "okx.public.ticker": Decimal("75000"),
            "binance.futures.public": Decimal("75010"),
            "cmc.quotes": Decimal("75005"),
        },
    )
    assert result.trade_critical_ok
    assert result.chosen_source == "okx.public.ticker"  # we fill there, so it wins ties
    assert result.price == Decimal("75000")
    assert result.outliers == {}


def test_a_single_outlier_is_excluded_but_consensus_survives() -> None:
    result = consensus_price(
        "BTC",
        {
            "okx.public.ticker": Decimal("75000"),
            "binance.futures.public": Decimal("75010"),
            "bybit.kz.public": Decimal("74990"),
            "cmc.quotes": Decimal("73000"),  # ~2.7% off, the F-010 signature
        },
    )
    assert result.trade_critical_ok
    assert "cmc.quotes" in result.outliers
    assert "cmc.quotes" not in result.agreeing


def test_scattered_sources_block_a_trade_critical_price() -> None:
    result = consensus_price(
        "BTC",
        {
            "okx.public.ticker": Decimal("75000"),
            "cmc.quotes": Decimal("73500"),
            "coingecko.coin": Decimal("76500"),
        },
    )
    assert not result.trade_critical_ok
    assert result.event is not None
    assert result.event.severity == "reject"
    assert result.price is not None  # still reported, just not tradable


def test_two_sources_are_never_enough_however_close() -> None:
    result = consensus_price(
        "BTC", {"okx.public.ticker": Decimal("75000"), "binance.futures.public": Decimal("75001")}
    )
    assert not result.trade_critical_ok
    assert result.event is not None and "needs >=3" in result.event.detail


def test_no_usable_price_is_reported_not_guessed() -> None:
    result = consensus_price("BTC", {"okx.public.ticker": Decimal("0")})
    assert result.price is None
    assert result.event is not None
