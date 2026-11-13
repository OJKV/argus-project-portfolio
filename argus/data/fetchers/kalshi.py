"""Kalshi public market data (DATA_SOURCES.md §4). Tier 1, no key.

Regulated US event contracts. The Polymarket ↔ Kalshi ↔ futures spread is the "crowd
disagreement" signal; a single venue's odds are not.
"""

from __future__ import annotations

from typing import Any

from argus.data.envelope import Observation, Tier, finite_number
from argus.data.http import fetch_json
from argus.data.scrub import scrub

SOURCE = "kalshi"
TIER: Tier = 1
API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"fed": "KXFED", "btc_daily": "KXBTCD"}


def fetch_markets(series_ticker: str, limit: int = 50) -> Observation:
    return fetch_json(
        f"{SOURCE}.markets.{series_ticker}",
        f"{API}/markets",
        TIER,
        "macro",
        params={"series_ticker": series_ticker, "status": "open", "limit": limit},
    )


def _probability(market: dict[str, Any]) -> float | None:
    """Kalshi quotes in dollars (0–1) on newer fields and cents on older ones."""
    for key in ("last_price_dollars", "yes_bid_dollars"):
        value = market.get(key)
        if value not in (None, ""):
            number = finite_number(value)
            if number is not None:
                return number
    for key in ("last_price", "yes_bid"):
        value = market.get(key)
        number = finite_number(value)
        if number is not None and number > 0:
            return number / 100
    return None


def market_odds(obs: Observation) -> list[dict[str, Any]]:
    payload = obs.data if isinstance(obs.data, dict) else {}
    rows: list[dict[str, Any]] = []
    for market in payload.get("markets") or []:
        probability = _probability(market)
        if probability is None:
            continue
        rows.append(
            {
                "ticker": market.get("ticker"),
                "title": scrub(market.get("title") or market.get("subtitle")).text,
                "yes_probability": probability,
                "close_time": market.get("close_time"),
            }
        )
    return rows
