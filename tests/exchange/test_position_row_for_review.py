"""What `position_row` hands the Reviewer — which is what reaches eight free LLM providers.

Review 2026-10-07 L-3: the row carried no time in trade, so a 72 h plan ten days old was
HOLD-able forever. ccxt maps OKX's `cTime` to `timestamp`; the row now says when it opened.
"""

from __future__ import annotations

from typing import Any

from argus.exchange.okx import position_row

RECORDED: dict[str, Any] = {  # trimmed ccxt row for an OKX USDT-margined swap
    "symbol": "BTC/USDT:USDT",
    "side": "long",
    "contracts": "0.5",
    "entryPrice": "100000",
    "markPrice": "99000",
    "leverage": "10",
    "liquidationPrice": "91000",
    "unrealizedPnl": "-500",
    "timestamp": 1758794400000,  # OKX cTime, ms
    "info": {
        "mgnRatio": "128.62",
        "instId": "BTC-USDT-SWAP",
        "cTime": "1758794400000",
        "posId": "1234567890",
        "uid": "77777777",
    },
}


def test_the_row_says_when_the_position_opened() -> None:
    assert position_row(RECORDED)["opened_at"] == "2025-09-25T10:00:00+00:00"


def test_an_unknown_open_time_is_none_not_now() -> None:
    assert position_row({**RECORDED, "timestamp": None})["opened_at"] is None


def test_the_raw_venue_row_is_not_carried() -> None:
    """Review L-12: `raw` was OKX's whole position object, account and position ids included,
    and the Reviewer serialises this row into a prompt for free providers and Logfire."""
    row = position_row(RECORDED)
    assert "raw" not in row
    assert "77777777" not in repr(row) and "1234567890" not in repr(row)
