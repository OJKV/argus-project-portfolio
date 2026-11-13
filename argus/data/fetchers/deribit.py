"""Deribit public: DVOL and option positioning (DATA_SOURCES.md §1). Tier 1, no key.

Feeds the volatility side of the regime and flags quarterly expiries as catalysts.
Blocked from Kazakhstan without a VPN (same ISP block as www.okx.com) — degrades to `missing`.
"""

from __future__ import annotations

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "deribit"
TIER: Tier = 1
HOST = "https://www.deribit.com"


def fetch_dvol(currency: str = "BTC", resolution: str = "3600") -> Observation:
    """Deribit's implied-volatility index — the market's own read on coming movement."""
    return fetch_json(
        f"{SOURCE}.dvol",
        f"{HOST}/api/v2/public/get_volatility_index_data",
        TIER,
        "derivatives",
        params={
            "currency": currency,
            "resolution": resolution,
            "start_timestamp": 0,
            "end_timestamp": 9_999_999_999_999,
        },
    )


def fetch_book_summary(currency: str = "BTC") -> Observation:
    """Per-instrument option summary — the raw material for put/call OI skew."""
    return fetch_json(
        f"{SOURCE}.book_summary",
        f"{HOST}/api/v2/public/get_book_summary_by_currency",
        TIER,
        "derivatives",
        params={"currency": currency, "kind": "option"},
    )


def fetch_index_price(index_name: str = "btc_usd") -> Observation:
    return fetch_json(
        f"{SOURCE}.index_price",
        f"{HOST}/api/v2/public/get_index_price",
        TIER,
        "price",
        params={"index_name": index_name},
    )
