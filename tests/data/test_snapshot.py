"""Snapshot assembly: families, provenance, gaps and degradation (ARCHITECTURE.md §4)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from argus.data import cache, http
from argus.data.snapshot import build_snapshot, symbol_mapping

OKX_TICKER: dict[str, Any] = {
    "code": "0",
    "data": [
        {
            "instId": "BTC-USDT-SWAP",
            "last": "75000",
            "high24h": "76000",
            "low24h": "74000",
            "bidPx": "74999.9",
            "askPx": "75000.1",
            "volCcy24h": "100000",
            "ts": "1789560000000",
        }
    ],
}
OKX_CANDLES = {"code": "0", "data": [["1789560000000", "1", "2", "0.5", "1", "1", "1", "0", "1"]]}
OKX_FUNDING = {"code": "0", "data": [{"fundingRate": "0.0001", "ts": "1789560000000"}]}
OKX_OI = {"code": "0", "data": [{"oiCcy": "2850", "ts": "1789560000000"}]}
RUBIK = {"code": "0", "data": [["1789560000000", "1.24"]]}
BYBIT = {"retCode": 0, "result": {"list": [{"lastPrice": "75010"}]}}
BINANCE = {"symbol": "BTCUSDT", "markPrice": "74995"}
HL: list[Any] = [{"universe": [{"name": "BTC"}]}, [{"funding": "0.0000125", "markPx": "75005"}]]


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    cache.clear()
    http.host_breakers.breakers.clear()
    yield
    cache.clear()
    http.host_breakers.breakers.clear()


def _mock_others() -> None:
    """Every non-OKX source."""
    respx.get(url__regex=r".*bybit.*").mock(return_value=httpx.Response(200, json=BYBIT))
    respx.get(url__regex=r".*/fapi/v1/premiumIndex").mock(
        return_value=httpx.Response(200, json=BINANCE)
    )
    respx.post(url__regex=r".*hyperliquid.*").mock(return_value=httpx.Response(200, json=HL))


def _mock_okx() -> None:
    respx.get(url__regex=r".*/api/v5/market/ticker\b").mock(
        return_value=httpx.Response(200, json=OKX_TICKER)
    )
    respx.get(url__regex=r".*/api/v5/market/candles").mock(
        return_value=httpx.Response(200, json=OKX_CANDLES)
    )
    respx.get(url__regex=r".*/public/funding-rate").mock(
        return_value=httpx.Response(200, json=OKX_FUNDING)
    )
    respx.get(url__regex=r".*/public/open-interest").mock(
        return_value=httpx.Response(200, json=OKX_OI)
    )
    respx.get(url__regex=r".*/rubik/.*").mock(return_value=httpx.Response(200, json=RUBIK))


def _mock_all() -> None:
    _mock_okx()
    _mock_others()


def test_symbol_mapping_uses_the_universe_file_for_core_assets() -> None:
    ids = symbol_mapping("BTC")
    assert ids["okx"] == "BTC-USDT-SWAP"
    assert ids["coingecko"] == "bitcoin"
    assert symbol_mapping("gram")["okx"] == "GRAM-USDT-SWAP"  # renamed asset resolves


@respx.mock
def test_a_complete_snapshot_is_tradable_and_carries_provenance() -> None:
    _mock_all()
    snap = build_snapshot("BTC")
    assert snap.technical["last"] == Decimal("75000")
    assert snap.derivatives["funding_rate_8h"] == Decimal("0.0001")
    assert snap.derivatives["hyperliquid_funding"] == Decimal("0.0000125")
    assert snap.data_gaps == []
    assert snap.tradable
    assert len(snap.provenance) == 8
    assert all(o.tier == 1 for o in snap.provenance)


@respx.mock
def test_price_consensus_prefers_the_venue_we_fill_on() -> None:
    _mock_all()
    snap = build_snapshot("BTC")
    assert snap.consensus is not None
    assert snap.consensus.chosen_source == "okx.public.ticker"
    assert len(snap.consensus.agreeing) == 4


@respx.mock
def test_missing_derivatives_makes_the_snapshot_untradable() -> None:
    _mock_all()
    respx.get(url__regex=r".*/public/funding-rate").mock(return_value=httpx.Response(503))
    respx.get(url__regex=r".*/public/open-interest").mock(return_value=httpx.Response(503))
    snap = build_snapshot("BTC")
    assert not snap.tradable
    assert any("derivatives" in gap for gap in snap.data_gaps)


@respx.mock
def test_okx_down_still_yields_a_price_from_the_other_venues() -> None:
    # The degradation the design promises: OKX blocked, three venues still agree.
    # The blocked route is registered first because respx matches in registration order.
    respx.get(url__regex=r".*/api/v5/.*").mock(side_effect=httpx.ConnectTimeout("blocked"))
    respx.get(url__regex=r".*/rubik/.*").mock(side_effect=httpx.ConnectTimeout("blocked"))
    _mock_others()
    snap = build_snapshot("BTC")
    assert not snap.tradable  # technical family is gone, so no trade
    assert snap.consensus is not None and snap.consensus.price is not None
    assert "bybit.kz.public" in snap.consensus.agreeing


@respx.mock
def test_a_price_outside_its_own_24h_range_raises_a_quality_event() -> None:
    _mock_all()
    row: dict[str, str] = {**OKX_TICKER["data"][0], "last": "80000"}
    stale = {"code": "0", "data": [row]}
    respx.get(url__regex=r".*/api/v5/market/ticker\b").mock(
        return_value=httpx.Response(200, json=stale)
    )
    snap = build_snapshot("BTC")
    assert any(e.trap == "trap_02_range_sanity" for e in snap.quality_events)


def test_whitelisted_assets_resolve_their_verified_slugs() -> None:
    # Without this, a whitelisted asset fell through to the generic fallback with no
    # CoinGecko id, and every fundamental fetch for it would have come back empty.
    assert symbol_mapping("FIL")["coingecko"] == "filecoin"
    assert symbol_mapping("ENA")["coingecko"] == "ethena"
    assert symbol_mapping("aave")["okx"] == "AAVE-USDT-SWAP"
