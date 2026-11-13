"""CoinMarketCap — cross-check only (DATA_SOURCES.md §2). Tier 1 as a *second* opinion.

Never the universe source (micro-cap bias) and never a primary price: some tables strip
+/− signs (trap 4) and quotes can run ~24h stale.
"""

from __future__ import annotations

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "cmc"
TIER: Tier = 1
HOST = "https://pro-api.coinmarketcap.com"


def fetch_quotes(symbols: str) -> Observation:
    key = get_settings().cmc_api_key.get_secret_value()
    if not key:
        return Observation.missing(f"{SOURCE}.quotes", TIER, "CMC_API_KEY not set")
    return fetch_json(
        f"{SOURCE}.quotes",
        f"{HOST}/v2/cryptocurrency/quotes/latest",
        TIER,
        "price",
        params={"symbol": symbols, "convert": "USD"},
        headers={"X-CMC_PRO_API_KEY": key},
    )
