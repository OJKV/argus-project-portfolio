"""Alternative.me Fear & Greed index (DATA_SOURCES.md §2). Tier 1, no key. Regime input."""

from __future__ import annotations

from typing import Any

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json, s_to_utc

SOURCE = "fear_greed"
TIER: Tier = 1
URL = "https://api.alternative.me/fng/"


def fetch_fear_greed(limit: int = 30) -> Observation:
    obs = fetch_json(f"{SOURCE}.index", URL, TIER, "news", params={"limit": limit})
    if isinstance(obs.data, dict) and obs.data.get("data"):
        newest: dict[str, Any] = obs.data["data"][0]
        return obs.model_copy(update={"asof": s_to_utc(newest.get("timestamp"))})
    return obs
