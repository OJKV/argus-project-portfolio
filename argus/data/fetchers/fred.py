"""FRED (St. Louis Fed) series (DATA_SOURCES.md §4). Tier 1, free key.

`DFII10` (10-year TIPS real yield) is the regime's risk-off tripwire. FRED marks missing days
with the string ".", which is *missing*, never zero (trap 3). Lags one business day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "fred"
TIER: Tier = 1
API = "https://api.stlouisfed.org/fred/series/observations"
SERIES = {
    "real_yield_10y": "DFII10",
    "yield_10y": "DGS10",
    "dollar_broad": "DTWEXBGS",
    "fed_funds": "FEDFUNDS",
    "curve_10y2y": "T10Y2Y",
    "vix": "VIXCLS",
}


# How old the newest print may be before the series counts as stopped updating. Daily series
# lag one business day, so a week covers a weekend, a holiday and the lag; the broad dollar
# index is released weekly; fed funds is a monthly average published early the next month.
MAX_AGE = {"DTWEXBGS": timedelta(days=14), "FEDFUNDS": timedelta(days=70)}
DAILY_MAX_AGE = timedelta(days=7)


def fetch_series(series_id: str, limit: int = 30, now: datetime | None = None) -> Observation:
    """The series, stamped with its newest observation date and marked `stale` when that date
    is older than the series' cadence allows (D-7: the date used to be discarded, so a series
    that had stopped updating read as current)."""
    key = get_settings().fred_api_key.get_secret_value()
    if not key:
        return Observation.missing(f"{SOURCE}.{series_id}", TIER, "FRED_API_KEY not set")
    obs = _fetch(series_id, key, limit)
    _, day = latest_value(obs)
    if day is None:
        return obs
    stamped = obs.model_copy(update={"asof": day})
    return stamped.mark_stale(MAX_AGE.get(series_id, DAILY_MAX_AGE), now or datetime.now(UTC))


def _fetch(series_id: str, key: str, limit: int) -> Observation:
    return fetch_json(
        f"{SOURCE}.{series_id}",
        API,
        TIER,
        "macro",
        params={
            "series_id": series_id,
            "api_key": key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        },
    )


def latest_value(obs: Observation) -> tuple[float | None, datetime | None]:
    """Newest non-missing observation and its date."""
    if not isinstance(obs.data, dict):
        return None, None
    for row in obs.data.get("observations") or []:
        raw = row.get("value")
        if raw in (None, "", "."):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        try:
            day = datetime.strptime(str(row.get("date")), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            day = None
        return value, day
    return None, None
