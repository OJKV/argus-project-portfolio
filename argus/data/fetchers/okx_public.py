"""OKX v5 public: candles, ticker, funding, open interest (DATA_SOURCES.md §1). Tier 1.

OKX is the primary price source because it is where we fill. The public host is configurable
so market data still works when the authenticated host is unreachable (F-034).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json, ms_to_utc, with_venue_asof

SOURCE = "okx.public"
TIER: Tier = 1


def _host() -> str:
    return get_settings().okx_public_host.rstrip("/")


def _first(observation: Observation) -> dict[str, Any] | None:
    payload = observation.data
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        if rows:
            first: dict[str, Any] = rows[0]
            return first
    return None


def fetch_candles(inst_id: str, bar: str = "1H", limit: int = 300) -> Observation:
    """OHLCV, newest first. The last row may be the *forming* candle — callers must drop it."""
    obs = fetch_json(
        f"{SOURCE}.candles.{bar}",
        f"{_host()}/api/v5/market/candles",
        TIER,
        "price",
        params={"instId": inst_id, "bar": bar, "limit": limit},
    )
    if isinstance(obs.data, dict) and isinstance(obs.data.get("data"), list):
        return obs.model_copy(update={"asof": last_closed_at(obs.data["data"], bar)})
    return obs


_BAR_SECONDS = {"m": 60, "H": 3600, "D": 86400, "W": 604800}


def last_closed_at(rows: list[Any], bar: str) -> datetime | None:
    """Close time of the newest *closed* bar — the data time of every close-based number.

    Stamping from the newest row (the forming bar, which `candles_to_frame` drops) made a gap
    or a suspended instrument look fresh (D-N4). A bar length we cannot parse (months) falls
    back to the bar's open time: older than the truth, never fresher.
    """
    match = re.fullmatch(r"(\d+)([mHDW])(?:utc)?", bar)
    length = timedelta(seconds=int(match[1]) * _BAR_SECONDS[match[2]]) if match else timedelta()
    for row in rows:
        if isinstance(row, list) and row and (len(row) < 9 or str(row[8]) == "1"):
            opened = ms_to_utc(row[0])
            return opened + length if opened is not None else None
    return None


def fetch_ticker(inst_id: str) -> Observation:
    obs = fetch_json(
        f"{SOURCE}.ticker",
        f"{_host()}/api/v5/market/ticker",
        TIER,
        "price",
        params={"instId": inst_id},
    )
    row = _first(obs)
    return with_venue_asof(obs, ms_to_utc(row.get("ts")), "price") if row else obs


def fetch_funding(inst_id: str) -> Observation:
    obs = fetch_json(
        f"{SOURCE}.funding",
        f"{_host()}/api/v5/public/funding-rate",
        TIER,
        "derivatives",
        params={"instId": inst_id},
    )
    row = _first(obs)
    return with_venue_asof(obs, ms_to_utc(row.get("ts")), "derivatives") if row else obs


def fetch_funding_history(inst_id: str, limit: int = 100) -> Observation:
    obs = fetch_json(
        f"{SOURCE}.funding_history",
        f"{_host()}/api/v5/public/funding-rate-history",
        TIER,
        "derivatives",
        params={"instId": inst_id, "limit": limit},
    )
    rows = obs.data.get("data") if isinstance(obs.data, dict) else None
    stamps = [ms_to_utc(r.get("fundingTime")) for r in rows or [] if isinstance(r, dict)]
    known = [s for s in stamps if s is not None]
    # D-7: the newest settlement is the series' data time; it had none before.
    return obs.model_copy(update={"asof": max(known)}) if known else obs


def fetch_open_interest(inst_id: str) -> Observation:
    obs = fetch_json(
        f"{SOURCE}.open_interest",
        f"{_host()}/api/v5/public/open-interest",
        TIER,
        "derivatives",
        params={"instType": "SWAP", "instId": inst_id},
    )
    row = _first(obs)
    return with_venue_asof(obs, ms_to_utc(row.get("ts")), "derivatives") if row else obs


def fetch_tickers() -> Observation:
    """All SWAP tickers — the universe screen's liquidity input."""
    obs = fetch_json(
        f"{SOURCE}.tickers",
        f"{_host()}/api/v5/market/tickers",
        TIER,
        "price",
        params={"instType": "SWAP"},
    )
    rows = obs.data.get("data") if isinstance(obs.data, dict) else None
    stamps = [ms_to_utc(r.get("ts")) for r in rows or [] if isinstance(r, dict)]
    known = [s for s in stamps if s is not None]
    return with_venue_asof(obs, max(known), "price") if known else obs
