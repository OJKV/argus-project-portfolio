"""Polymarket Gamma API: event odds (DATA_SOURCES.md §4). Tier 1, no key. Advisory only (Q11).

Market questions are third-party text, so they are scrubbed. `outcomePrices` arrives as a JSON
array *inside a string* — parsed defensively, and a malformed price is dropped, never zeroed.
"""

from __future__ import annotations

import json
from typing import Any

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json
from argus.data.scrub import scrub

SOURCE = "polymarket"
TIER: Tier = 1
API = "https://gamma-api.polymarket.com"
WATCH_TAGS = ("fed-rates", "bitcoin")


def fetch_events(tag_slug: str, limit: int = 10) -> Observation:
    """Active events for a tag, most-traded first."""
    return fetch_json(
        f"{SOURCE}.events.{tag_slug}",
        f"{API}/events",
        TIER,
        "macro",
        params={
            "tag_slug": tag_slug,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": limit,
        },
    )


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def market_odds(obs: Observation) -> list[dict[str, Any]]:
    """Flatten events → markets → {question, outcome: probability}."""
    if not isinstance(obs.data, list):
        return []
    rows: list[dict[str, Any]] = []
    for event in obs.data:
        for market in event.get("markets") or []:
            outcomes = _as_list(market.get("outcomes"))
            prices = _as_list(market.get("outcomePrices"))
            odds: dict[str, float] = {}
            for name, price in zip(outcomes, prices, strict=False):
                try:
                    probability = float(price)
                except (TypeError, ValueError):
                    continue
                if 0.0 <= probability <= 1.0:
                    odds[scrub(str(name), limit=60).text] = probability
            if not odds:
                continue
            question = scrub(market.get("question"))
            rows.append(
                {
                    "event": scrub(event.get("title")).text,
                    "question": question.text,
                    "injection_suspect": question.injection_suspect,
                    "odds": odds,
                    "volume_24h": market.get("volume24hr"),
                    "end": market.get("endDate"),
                }
            )
    return rows
