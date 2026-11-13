"""Review 2026-09-18 X-7: the margin-ratio floor was dead code in production.

`_margin_check` reads `position["margin_ratio"]` and skips when it is None — and neither the OKX
adapter nor the fake ever emitted that key, so the check has never once run against a real
position. A margin call would arrive with no warning ahead of it.
"""

from __future__ import annotations

from decimal import Decimal

from argus.exchange.okx import position_row

RECORDED = {  # trimmed ccxt row for an OKX USDT-margined swap
    "symbol": "BTC/USDT:USDT",
    "side": "long",
    "contracts": "0.5",
    "entryPrice": "100000",
    "markPrice": "99000",
    "leverage": "10",
    "liquidationPrice": "91000",
    "unrealizedPnl": "-500",
    "info": {"mgnRatio": "128.62", "instId": "BTC-USDT-SWAP"},
}


def test_margin_ratio_is_carried_through_from_okx_as_a_percent() -> None:
    """F-164: OKX's `mgnRatio` is a multiple (adjEq / mmr, docs-v5), not a percent. The gate and
    watchdog floors are percents (200%), so 128.62x must reach them as 12862%. Read as "128%",
    a healthy isolated 2x demo position (live, 2026-10-27: mgnRatio 111) blocked every entry."""
    assert position_row(RECORDED)["margin_ratio"] == Decimal("12862")


def test_a_row_without_a_margin_ratio_reports_none_not_zero() -> None:
    row = position_row({**RECORDED, "info": {}})
    assert row["margin_ratio"] is None  # unknown is not "healthy", and it is not "at liquidation"


def test_the_other_fields_still_map() -> None:
    row = position_row(RECORDED)
    assert row["size"] == Decimal("0.5")
    assert row["entry_price"] == Decimal("100000")
    assert row["leverage"] == 10
    assert row["liq_price"] == Decimal("91000")
