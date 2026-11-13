"""Equity for sizing = the settlement currency only (live finding 2026-09-17, FAILURE_LOG F-047).

The ARGUS demo sub-account holds 1 BTC, 1 ETH, 100 OKB and 5,000 USDT. OKX's `totalEq` values all
of it at $95.5k, but in Single-currency margin mode a USDT-margined perp can only use the USDT.
Sizing 1% risk on `totalEq` would risk $955 of a $5,000 margin pool — 19x the intended bet —
and the drawdown kill-switch would fire on BTC spot moves instead of trading losses.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from argus.exchange.okx import ExchangeResponseError, settlement_balance

RECORDED = {  # trimmed from GET /api/v5/account/balance on the demo sub-account
    "totalEq": "95495.70999999999",
    "details": [
        {"ccy": "BTC", "eq": "1", "availEq": "1", "availBal": "1", "eqUsd": "76804.9"},
        {"ccy": "OKB", "eq": "100", "availEq": "100", "availBal": "100", "eqUsd": "11222.6"},
        {"ccy": "USDT", "eq": "5000", "availEq": "5000", "availBal": "5000", "eqUsd": "4995.15"},
        {"ccy": "ETH", "eq": "1", "availEq": "1", "availBal": "1", "eqUsd": "2473.06"},
    ],
}


def test_equity_is_the_usdt_pool_not_the_whole_account() -> None:
    balance = settlement_balance(RECORDED, "USDT")
    assert balance["equity"] == Decimal("5000")
    assert balance["free"] == Decimal("5000")
    assert balance["account_total_usd"] == Decimal("95495.70999999999")  # reported, never sized on


def test_open_position_pnl_and_used_margin_are_reflected() -> None:
    row = {"ccy": "USDT", "eq": "4950.5", "availEq": "4700.25", "availBal": "4800"}
    balance = settlement_balance({"totalEq": "4950.5", "details": [row]}, "USDT")
    assert balance["equity"] == Decimal("4950.5")  # eq includes unrealised PnL
    assert balance["free"] == Decimal("4700.25")  # availEq excludes margin in use


def test_free_falls_back_to_available_balance_when_avail_eq_is_blank() -> None:
    row = {"ccy": "USDT", "eq": "100", "availEq": "", "availBal": "90"}
    assert settlement_balance({"details": [row]}, "USDT")["free"] == Decimal("90")


def test_no_usdt_never_falls_back_to_the_other_coins() -> None:
    """Originally asserted equity 0. Review X-3 showed 0 is not a safe answer either: the
    drawdown kill-switch reads it as a 100% loss and flattens the account. The rule is now the
    same as F-030's — a missing settlement row is an error, and it is certainly not $1,000 of BTC.
    """
    raw = {"totalEq": "1000", "details": [{"ccy": "BTC", "eq": "0.01", "availEq": "0.01"}]}
    with pytest.raises(ExchangeResponseError):
        settlement_balance(raw, "USDT")


def test_a_malformed_response_is_refused() -> None:
    with pytest.raises(ExchangeResponseError, match="details"):
        settlement_balance({"totalEq": "1"}, "USDT")
    with pytest.raises(ExchangeResponseError, match="eq"):
        settlement_balance({"details": [{"ccy": "USDT", "eq": ""}]}, "USDT")


def test_a_missing_settlement_row_is_an_error_not_zero_equity() -> None:
    """Review 2026-09-18 X-3: zero equity reads as a 100% drawdown and flattens the account.

    A balance payload that simply does not mention USDT (a glitch, a wrong sub-account, a
    momentary API hiccup) returned `equity=0`. The watchdog then computed a 100% drawdown from the
    30-day peak, closed every position at market and wrote HALT — from one bad reading.
    """
    payload = {"totalEq": "5000", "details": [{"ccy": "BTC", "eq": "1", "availEq": "1"}]}
    with pytest.raises(ExchangeResponseError) as exc:
        settlement_balance(payload, "USDT")
    assert "USDT" in str(exc.value)


def test_a_zero_settlement_row_is_also_an_error() -> None:
    payload = {"totalEq": "0", "details": [{"ccy": "USDT", "eq": "0", "availEq": "0"}]}
    with pytest.raises(ExchangeResponseError):
        settlement_balance(payload, "USDT")
