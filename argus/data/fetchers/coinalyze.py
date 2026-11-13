"""Coinalyze: aggregated derivatives across venues (DATA_SOURCES.md §1). Tier 1, free key.

Best free aggregate for OI, funding, predicted funding and liquidations. Two standing rules:
use the API (it returns `t`; the web columns carry no timestamp — trap 7), and never mix it
with another vendor inside one series (trap 9, F-022).
"""

from __future__ import annotations

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "coinalyze"
TIER: Tier = 1
HOST = "https://api.coinalyze.net/v1"


def _headers() -> dict[str, str] | None:
    key = get_settings().coinalyze_api_key.get_secret_value()
    return {"api_key": key} if key else None


def _series(path: str, name: str, symbols: str, interval: str, frm: int, to: int) -> Observation:
    if _headers() is None:
        return Observation.missing(f"{SOURCE}.{name}", TIER, "COINALYZE_API_KEY not set")
    return fetch_json(
        f"{SOURCE}.{name}",
        f"{HOST}{path}",
        TIER,
        "derivatives",
        params={"symbols": symbols, "interval": interval, "from": frm, "to": to},
        headers=_headers(),
    )


def fetch_open_interest_history(symbols: str, interval: str, frm: int, to: int) -> Observation:
    return _series("/open-interest-history", "open_interest_history", symbols, interval, frm, to)


def fetch_funding_rate_history(symbols: str, interval: str, frm: int, to: int) -> Observation:
    return _series("/funding-rate-history", "funding_rate_history", symbols, interval, frm, to)


def fetch_predicted_funding_history(symbols: str, interval: str, frm: int, to: int) -> Observation:
    return _series(
        "/predicted-funding-rate-history", "predicted_funding", symbols, interval, frm, to
    )


def fetch_liquidation_history(symbols: str, interval: str, frm: int, to: int) -> Observation:
    return _series("/liquidation-history", "liquidation_history", symbols, interval, frm, to)


def fetch_long_short_ratio_history(symbols: str, interval: str, frm: int, to: int) -> Observation:
    return _series("/long-short-ratio-history", "long_short_ratio", symbols, interval, frm, to)
