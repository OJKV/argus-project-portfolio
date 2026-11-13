"""Binance USDⓈ-M futures public (DATA_SOURCES.md §1). Tier 1.

Deep OHLCV history for backtests plus positioning series OKX does not publish. The
`/futures/data/*` endpoints keep only ~30 days, so they are signal, never history.
Geo-blocking is possible from some VPS regions — the fetchers degrade to `missing`.
"""

from __future__ import annotations

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json, ms_to_utc, with_venue_asof

SOURCE = "binance.futures.public"
TIER: Tier = 1
HOST = "https://fapi.binance.com"


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 500) -> Observation:
    return fetch_json(
        f"{SOURCE}.klines",
        f"{HOST}/fapi/v1/klines",
        TIER,
        "price",
        params={"symbol": symbol, "interval": interval, "limit": limit},
    )


def fetch_premium_index(symbol: str, *, force: bool = False) -> Observation:
    """Mark price, index price and current funding in one call. `force` bypasses the cache."""
    obs = fetch_json(
        f"{SOURCE}.premium_index",
        f"{HOST}/fapi/v1/premiumIndex",
        TIER,
        "derivatives",
        params={"symbol": symbol},
        force=force,
    )
    stamp = obs.data.get("time") if isinstance(obs.data, dict) else None
    return with_venue_asof(obs, ms_to_utc(stamp), "derivatives")


def fetch_open_interest_hist(symbol: str, period: str = "1h", limit: int = 30) -> Observation:
    return fetch_json(
        f"{SOURCE}.open_interest_hist",
        f"{HOST}/futures/data/openInterestHist",
        TIER,
        "derivatives",
        params={"symbol": symbol, "period": period, "limit": limit},
    )


def fetch_top_trader_position_ratio(
    symbol: str, period: str = "1h", limit: int = 30
) -> Observation:
    """Top traders by position — the positioning series retail ratios cannot see."""
    return fetch_json(
        f"{SOURCE}.top_long_short_position",
        f"{HOST}/futures/data/topLongShortPositionRatio",
        TIER,
        "derivatives",
        params={"symbol": symbol, "period": period, "limit": limit},
    )


def fetch_taker_long_short_ratio(symbol: str, period: str = "1h", limit: int = 30) -> Observation:
    return fetch_json(
        f"{SOURCE}.taker_long_short",
        f"{HOST}/futures/data/takerlongshortRatio",
        TIER,
        "derivatives",
        params={"symbol": symbol, "period": period, "limit": limit},
    )
