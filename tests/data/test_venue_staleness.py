"""A venue timestamp older than the family's cache TTL reads `stale`, not `ok` (Muse P1-2).

The bound is the existing `cache.TTL_SECONDS[family]`; no new number. Age is measured against
`observed_at` (when we fetched), so a cache hit does not make fresh data look old.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from argus.data import http
from argus.data.cache import TTL_SECONDS
from argus.data.envelope import Observation
from argus.data.fetchers import binance_futures_public, bybit_kz_public, coingecko, okx_public

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(seconds=TTL_SECONDS["price"] + 60)
FRESH = NOW - timedelta(seconds=30)


def _ms(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


def _obs(data: Any, source: str = "x") -> Observation:
    return Observation(source=source, tier=1, observed_at=NOW, data=data)


def _patch(monkeypatch: pytest.MonkeyPatch, module: Any, data: Any) -> None:
    monkeypatch.setattr(module, "fetch_json", lambda *a, **k: _obs(data, a[0]))


def test_helper_marks_old_venue_time_stale_and_keeps_fresh_ok() -> None:
    old = http.with_venue_asof(_obs({}), OLD, "price")
    assert old.quality == "stale" and old.asof == OLD
    fresh = http.with_venue_asof(_obs({}), FRESH, "price")
    assert fresh.quality == "ok" and fresh.asof == FRESH
    assert http.with_venue_asof(_obs({}), None, "price").asof is None


def test_a_cache_hit_is_not_aged_by_the_wall_clock() -> None:
    cached_obs = Observation(source="x", tier=1, observed_at=NOW - timedelta(days=3), data={})
    assert (
        http.with_venue_asof(
            cached_obs, cached_obs.observed_at - timedelta(seconds=5), "price"
        ).quality
        == "ok"
    )


@pytest.mark.parametrize("fetch", ["fetch_ticker", "fetch_funding", "fetch_open_interest"])
def test_okx_snapshots_go_stale(monkeypatch: pytest.MonkeyPatch, fetch: str) -> None:
    _patch(monkeypatch, okx_public, {"data": [{"ts": _ms(OLD)}]})
    assert getattr(okx_public, fetch)("BTC-USDT-SWAP").quality == "stale"
    _patch(monkeypatch, okx_public, {"data": [{"ts": _ms(FRESH)}]})
    assert getattr(okx_public, fetch)("BTC-USDT-SWAP").quality == "ok"


def test_okx_tickers_use_the_newest_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, okx_public, {"data": [{"ts": _ms(OLD)}, {"ts": _ms(OLD)}]})
    assert okx_public.fetch_tickers().quality == "stale"
    _patch(monkeypatch, okx_public, {"data": [{"ts": _ms(OLD)}, {"ts": _ms(FRESH)}]})
    assert okx_public.fetch_tickers().quality == "ok"


def test_coingecko_coin_and_global(monkeypatch: pytest.MonkeyPatch) -> None:
    old_iso = OLD.isoformat().replace("+00:00", "Z")
    _patch(monkeypatch, coingecko, {"market_data": {"last_updated": old_iso}})
    assert coingecko.fetch_coin("bitcoin").quality == "stale"
    _patch(monkeypatch, coingecko, {"last_updated": FRESH.isoformat()})
    assert coingecko.fetch_coin("bitcoin").quality == "ok"
    _patch(monkeypatch, coingecko, {"data": {"updated_at": int(OLD.timestamp())}})
    assert coingecko.fetch_global().quality == "stale"
    _patch(monkeypatch, coingecko, {"data": {"updated_at": int(FRESH.timestamp())}})
    assert coingecko.fetch_global().quality == "ok"


def test_bybit_ticker_and_binance_premium_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, bybit_kz_public, {"time": int(OLD.timestamp() * 1000)})
    assert bybit_kz_public.fetch_ticker("BTCUSDT").quality == "stale"
    _patch(monkeypatch, binance_futures_public, {"time": int(OLD.timestamp() * 1000)})
    assert binance_futures_public.fetch_premium_index("BTCUSDT").quality == "stale"
    _patch(monkeypatch, binance_futures_public, {"time": int(FRESH.timestamp() * 1000)})
    assert binance_futures_public.fetch_premium_index("BTCUSDT").quality == "ok"


def test_a_missing_timestamp_stays_unstamped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, coingecko, {"market_data": {}})
    obs = coingecko.fetch_coin("bitcoin")
    assert obs.asof is None and obs.quality == "ok"
