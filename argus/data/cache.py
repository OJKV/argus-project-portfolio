"""Disk cache keyed by (source, params), with a per-source TTL (ARCHITECTURE.md §4).

Two things this buys beyond speed: a run that re-fetches the same series twice sees one value
(so two dossier fields can never disagree inside one run), and `argus replay` can re-run a
decision without touching the network.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import diskcache

from argus.config import REPO_ROOT, get_settings

# Cache TTLs by family (ARCHITECTURE.md §4 table).
TTL_SECONDS: dict[str, int] = {
    "price": 15 * 60,
    "derivatives": 15 * 60,
    "onchain": 4 * 60 * 60,
    "macro": 6 * 60 * 60,
    "flows": 6 * 60 * 60,
    "news": 2 * 60 * 60,
    "filings": 6 * 60 * 60,
    "reference": 24 * 60 * 60,
}
DEFAULT_TTL = 15 * 60


def cache_key(source: str, params: dict[str, Any] | None = None) -> str:
    blob = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    return f"{source}:{digest}"


def cache_dir() -> Path:
    home = get_settings().argus_home
    if not home.is_absolute():
        home = (REPO_ROOT / home).resolve()
    return home / "cache"


_cache: diskcache.Cache | None = None


def get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        path = cache_dir()
        path.mkdir(parents=True, exist_ok=True)
        _cache = diskcache.Cache(str(path))
    return _cache


# Entries are stored as (marker, fetched_at, value). The fetch time travels with the payload so
# a hit can say when the data was really fetched, not "now" (D-7). An entry without the marker
# predates this format and is treated as a miss.
_ENTRY = "argus.cache.v2"


def cached[T](
    source: str,
    params: dict[str, Any] | None,
    family: str,
    producer: Callable[[], T],
    force: bool = False,
) -> tuple[T, bool, datetime]:
    """Return (value, was_cached, fetched_at). `force` bypasses the read but still writes."""
    key = cache_key(source, params)
    store = get_cache()
    if not force:
        hit = store.get(key, default=None)
        if isinstance(hit, tuple) and len(hit) == 3 and hit[0] == _ENTRY:
            return hit[2], True, hit[1]
    value = producer()
    fetched_at = datetime.now(UTC)
    store.set(key, (_ENTRY, fetched_at, value), expire=TTL_SECONDS.get(family, DEFAULT_TTL))
    return value, False, fetched_at


def clear() -> None:
    get_cache().clear()
