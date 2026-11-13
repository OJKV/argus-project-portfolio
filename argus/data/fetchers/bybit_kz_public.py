"""Bybit (KZ host) public market data (DATA_SOURCES.md §1). Tier 1.

Measured 2026-09-15: api.bybit.kz shares the global matching engine — 870 identical linear
perps, BTCUSDT turnover and OI matching within 0.0008%. It is a regulated wrapper, not a thin
local pool, and it is reachable from Kazakhstan without a VPN — which makes it the natural
second opinion for price consensus when OKX is unreachable.
"""

from __future__ import annotations

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json, ms_to_utc, with_venue_asof

SOURCE = "bybit.kz.public"
TIER: Tier = 1


def _host() -> str:
    return get_settings().bybit_kz_host.rstrip("/")


def fetch_ticker(symbol: str, *, force: bool = False) -> Observation:
    """`force=True` bypasses the cache: the gate's price cross-check must be as fresh as OKX's."""
    obs = fetch_json(
        f"{SOURCE}.ticker",
        f"{_host()}/v5/market/tickers",
        TIER,
        "price",
        params={"category": "linear", "symbol": symbol},
        force=force,
    )
    stamp = obs.data.get("time") if isinstance(obs.data, dict) else None
    return with_venue_asof(obs, ms_to_utc(stamp), "price")


def fetch_kline(symbol: str, interval: str = "60", limit: int = 200) -> Observation:
    return fetch_json(
        f"{SOURCE}.kline",
        f"{_host()}/v5/market/kline",
        TIER,
        "price",
        params={"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
    )


def fetch_funding_history(symbol: str, limit: int = 100) -> Observation:
    return fetch_json(
        f"{SOURCE}.funding_history",
        f"{_host()}/v5/market/funding/history",
        TIER,
        "derivatives",
        params={"category": "linear", "symbol": symbol, "limit": limit},
    )


def fetch_open_interest(symbol: str, interval: str = "1h", limit: int = 50) -> Observation:
    return fetch_json(
        f"{SOURCE}.open_interest",
        f"{_host()}/v5/market/open-interest",
        TIER,
        "derivatives",
        params={"category": "linear", "symbol": symbol, "intervalTime": interval, "limit": limit},
    )
