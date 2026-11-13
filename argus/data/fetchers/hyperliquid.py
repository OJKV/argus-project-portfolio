"""Hyperliquid `info` endpoint (DATA_SOURCES.md §1). Tier 1, no key.

The largest on-chain perp venue. Its funding diverging from CEX funding is a positioning
signal a CEX-only view cannot see.
"""

from __future__ import annotations

from typing import Any

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "hyperliquid"
TIER: Tier = 1
URL = "https://api.hyperliquid.xyz/info"


def fetch_meta_and_contexts() -> Observation:
    """Universe metadata plus per-asset funding, OI, mark and premium in one POST."""
    return fetch_json(
        f"{SOURCE}.meta_and_asset_ctxs",
        URL,
        TIER,
        "derivatives",
        method="POST",
        json_body={"type": "metaAndAssetCtxs"},
    )


def asset_context(observation: Observation, coin: str) -> dict[str, Any] | None:
    """Pull one coin out of the paired [meta, contexts] response."""
    payload = observation.data
    if not isinstance(payload, list) or len(payload) != 2:
        return None
    universe = payload[0].get("universe", []) if isinstance(payload[0], dict) else []
    contexts = payload[1] if isinstance(payload[1], list) else []
    for index, asset in enumerate(universe):
        if asset.get("name", "").upper() == coin.upper() and index < len(contexts):
            context: dict[str, Any] = dict(contexts[index])
            context["name"] = asset.get("name")
            return context
    return None
