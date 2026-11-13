"""OKX rubik trading statistics: retail positioning proxies (DATA_SOURCES.md §1). Tier 1.

Levels here are not comparable across venues — only the **change** carries signal (trap 9).
"""

from __future__ import annotations

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json, ms_to_utc

SOURCE = "okx.rubik"
TIER: Tier = 1


def _host() -> str:
    return get_settings().okx_public_host.rstrip("/")


def _stamped(obs: Observation) -> Observation:
    """Rows are `[ts, value, ...]`; the newest bucket is the series' data time (D-7)."""
    rows = obs.data.get("data") if isinstance(obs.data, dict) else None
    stamps = [ms_to_utc(r[0]) for r in rows or [] if isinstance(r, list) and r]
    known = [s for s in stamps if s is not None]
    return obs.model_copy(update={"asof": max(known)}) if known else obs


def fetch_long_short_ratio(ccy: str, period: str = "1H") -> Observation:
    return _stamped(
        fetch_json(
            f"{SOURCE}.long_short_account_ratio",
            f"{_host()}/api/v5/rubik/stat/contracts/long-short-account-ratio",
            TIER,
            "derivatives",
            params={"ccy": ccy, "period": period},
        )
    )


def fetch_open_interest_volume(ccy: str, period: str = "1H") -> Observation:
    return _stamped(
        fetch_json(
            f"{SOURCE}.open_interest_volume",
            f"{_host()}/api/v5/rubik/stat/contracts/open-interest-volume",
            TIER,
            "derivatives",
            params={"ccy": ccy, "period": period},
        )
    )


def fetch_taker_volume(ccy: str, period: str = "1H") -> Observation:
    return _stamped(
        fetch_json(
            f"{SOURCE}.taker_volume",
            f"{_host()}/api/v5/rubik/stat/taker-volume",
            TIER,
            "derivatives",
            params={"ccy": ccy, "instType": "CONTRACTS", "period": period},
        )
    )
