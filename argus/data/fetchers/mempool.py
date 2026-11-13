"""mempool.space: BTC fee pressure and hashrate (DATA_SOURCES.md §3). Tier 1, no key.

Measured 2026-09-17: reachable directly from Kazakhstan, but refused from some commercial VPN
exits — the per-host breaker turns that into a `missing` family instead of a slow run.
"""

from __future__ import annotations

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "mempool"
TIER: Tier = 1
API = "https://mempool.space/api/v1"


def fetch_recommended_fees() -> Observation:
    return fetch_json(f"{SOURCE}.fees", f"{API}/fees/recommended", TIER, "onchain")


def fetch_hashrate_3d() -> Observation:
    return fetch_json(f"{SOURCE}.hashrate", f"{API}/mining/hashrate/3d", TIER, "onchain")
