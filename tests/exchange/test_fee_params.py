"""OKX trade-fee request params (live finding 2026-09-17, FAILURE_LOG F-041).

`GET /account/trade-fee` with `instType=SWAP&instId=BTC-USDT-SWAP` returns code 50016
"instId and instType don't match": for derivatives OKX wants `instFamily`, not `instId`.
The unit suite missed it because FakeExchange does not validate request params.
"""

from __future__ import annotations

from argus.exchange.okx import fee_rate_params


def test_swap_fee_request_uses_the_instrument_family() -> None:
    params = fee_rate_params(
        {"id": "BTC-USDT-SWAP", "type": "swap", "info": {"instFamily": "BTC-USDT"}}
    )
    assert params == {"instType": "SWAP", "instFamily": "BTC-USDT"}
    assert "instId" not in params


def test_family_is_derived_when_the_market_omits_it() -> None:
    params = fee_rate_params({"id": "ETH-USDT-SWAP", "type": "swap", "info": {}})
    assert params == {"instType": "SWAP", "instFamily": "ETH-USDT"}


def test_spot_fee_request_keeps_the_instrument_id() -> None:
    params = fee_rate_params({"id": "BTC-USDT", "type": "spot", "info": {}})
    assert params == {"instType": "SPOT", "instId": "BTC-USDT"}


# --- F-042: which fields hold the rate (recorded from OKX demo, 2026-09-17) ---------------

USDT_SWAP_ROW = {
    "instType": "SWAP",
    "level": "Lv1",
    "maker": "",  # coin-margined rate: empty when asked for a USDT family
    "taker": "",
    "makerU": "-0.0002",
    "takerU": "-0.0005",
    "makerUSDC": "",
    "takerUSDC": "",
}


def test_usdt_margined_swaps_read_the_u_fields() -> None:
    from decimal import Decimal

    from argus.exchange.okx import parse_fee_rates

    rates = parse_fee_rates(USDT_SWAP_ROW, settle="USDT")
    assert rates == {"maker": Decimal("-0.0002"), "taker": Decimal("-0.0005")}


def test_coin_margined_swaps_read_the_plain_fields() -> None:
    from decimal import Decimal

    from argus.exchange.okx import parse_fee_rates

    row = {**USDT_SWAP_ROW, "maker": "-0.0001", "taker": "-0.0004"}
    assert parse_fee_rates(row, settle="BTC")["taker"] == Decimal("-0.0004")


def test_an_empty_rate_is_an_error_never_a_zero_fee() -> None:
    import pytest

    from argus.exchange.okx import ExchangeResponseError, parse_fee_rates

    with pytest.raises(ExchangeResponseError, match="takerU"):
        parse_fee_rates({**USDT_SWAP_ROW, "takerU": ""}, settle="USDT")


def test_a_positive_taker_rate_is_refused() -> None:
    import pytest

    from argus.exchange.okx import ExchangeResponseError, parse_fee_rates

    with pytest.raises(ExchangeResponseError, match="positive"):
        parse_fee_rates({**USDT_SWAP_ROW, "takerU": "0.0005"}, settle="USDT")
