"""DefiLlama free API (DATA_SOURCES.md §3). Tier 1, no key.

Measured 2026-09-17: `/emissions` (token unlocks) and `bridges.llama.fi` now answer HTTP 402
"Upgrade to the paid API plan" — the unlock and bridge-flow families need another source
(FAILURE_LOG F-048). `/protocol/{slug}` returns ~10 MB for large protocols, so the lighter
`/tvl/{slug}` and the chart-less fee summaries are used instead.
"""

from __future__ import annotations

from typing import Any

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json, s_to_utc

SOURCE = "defillama"
TIER: Tier = 1
API = "https://api.llama.fi"
STABLES = "https://stablecoins.llama.fi"
SLIM = {"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"}


def fetch_chain_tvl_history(chain: str) -> Observation:
    """Daily TVL for a chain (~5 years). Callers compute Δ, never compare levels across vendors."""
    obs = fetch_json(f"{SOURCE}.chain_tvl", f"{API}/v2/historicalChainTvl/{chain}", TIER, "onchain")
    if isinstance(obs.data, list) and obs.data:
        return obs.model_copy(update={"asof": s_to_utc(obs.data[-1].get("date"))})
    return obs


def fetch_protocol_tvl(slug: str) -> Observation:
    """Current TVL. The API returns a bare number, so it arrives as `data["value"]`."""
    return fetch_json(f"{SOURCE}.protocol_tvl", f"{API}/tvl/{slug}", TIER, "onchain")


def fetch_protocol_fees(slug: str, data_type: str = "dailyFees") -> Observation:
    """24h/7d/30d fees (or `dailyRevenue`) with period-over-period change."""
    return fetch_json(
        f"{SOURCE}.fees.{data_type}",
        f"{API}/summary/fees/{slug}",
        TIER,
        "onchain",
        params={**SLIM, "dataType": data_type},
    )


def fetch_chain_fees(chain: str) -> Observation:
    return fetch_json(
        f"{SOURCE}.chain_fees", f"{API}/overview/fees/{chain}", TIER, "onchain", params=SLIM
    )


def fetch_stablecoin_supply() -> Observation:
    """Total stablecoin circulation over time — the market's dry-powder series."""
    obs = fetch_json(
        f"{SOURCE}.stablecoin_supply", f"{STABLES}/stablecoincharts/all", TIER, "onchain"
    )
    if isinstance(obs.data, list) and obs.data:
        return obs.model_copy(update={"asof": s_to_utc(obs.data[-1].get("date"))})
    return obs


def change_pct(series: list[dict[str, Any]], key: str, days: int) -> float | None:
    """Percent change of `key` over the last `days` daily points. None when history is short."""
    values = [row.get(key) for row in series]
    if len(values) <= days:
        return None
    first, last = values[-1 - days], values[-1]
    if first is None or last is None:
        return None
    try:
        old, new = float(first), float(last)
    except (TypeError, ValueError):
        return None
    if old == 0:
        return None
    return (new - old) / old * 100
