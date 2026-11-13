"""CoinGecko Demo API (DATA_SOURCES.md §2). Tier 1 for coin pages only.

Coin pages, never the homepage table: the homepage % column contradicts the coin pages
(trap 1) and was one of the sources that disagreed on BTC's direction (F-010).
"""

from __future__ import annotations

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json, iso_to_utc, s_to_utc, with_venue_asof

SOURCE = "coingecko"
TIER: Tier = 1
HOST = "https://api.coingecko.com/api/v3"


def _headers() -> dict[str, str]:
    key = get_settings().coingecko_demo_api_key.get_secret_value()
    return {"x-cg-demo-api-key": key} if key else {}


def fetch_coin(coin_id: str) -> Observation:
    """The coin page payload — market data, supply, FDV, categories."""
    obs = fetch_json(
        f"{SOURCE}.coin",
        f"{HOST}/coins/{coin_id}",
        TIER,
        "price",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
        },
        headers=_headers(),
    )
    payload = obs.data if isinstance(obs.data, dict) else {}
    market = payload.get("market_data")
    stamp = market.get("last_updated") if isinstance(market, dict) else None
    return with_venue_asof(obs, iso_to_utc(stamp or payload.get("last_updated")), "price")


def fetch_global() -> Observation:
    """Total market cap and BTC dominance — regime inputs."""
    obs = fetch_json(f"{SOURCE}.global", f"{HOST}/global", TIER, "price", headers=_headers())
    body = obs.data.get("data") if isinstance(obs.data, dict) else None
    stamp = body.get("updated_at") if isinstance(body, dict) else None
    return with_venue_asof(obs, s_to_utc(stamp), "price")
