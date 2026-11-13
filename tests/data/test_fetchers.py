"""Fetchers: envelope shape, provenance, degradation — against recorded payloads, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from argus.data import cache, http
from argus.data.envelope import Observation
from argus.data.fetchers import (
    binance_futures_public,
    bybit_kz_public,
    defillama,
    deribit,
    fear_greed,
    hyperliquid,
    okx_public,
    okx_rubik,
)

# Recorded 2026-09-16 from the live endpoints, trimmed to one row each.
OKX_TICKER: dict[str, Any] = {
    "code": "0",
    "data": [
        {
            "instId": "BTC-USDT-SWAP",
            "last": "75436.2",
            "high24h": "76800.0",
            "low24h": "74100.0",
            "volCcy24h": "113000",
            "bidPx": "75436.1",
            "askPx": "75436.2",
            "ts": "1789560000000",
        }
    ],
}
OKX_CANDLES: dict[str, Any] = {
    "code": "0",
    "data": [
        ["1789560000000", "75400", "75500", "75300", "75436.2", "1200", "90000000", "0", "0"],
        ["1789556400000", "75200", "75450", "75100", "75400", "1100", "82000000", "0", "1"],
    ],
}
OKX_FUNDING: dict[str, Any] = {
    "code": "0",
    "data": [
        {
            "instId": "BTC-USDT-SWAP",
            "fundingRate": "0.0000812",
            "nextFundingRate": "0.0001",
            "fundingTime": "1789574400000",
            "ts": "1789560000000",
        }
    ],
}
OKX_OI: dict[str, Any] = {
    "code": "0",
    "data": [{"instId": "BTC-USDT-SWAP", "oi": "285000", "oiCcy": "2850", "ts": "1789560000000"}],
}
FNG: dict[str, Any] = {
    "data": [{"value": "61", "value_classification": "Greed", "timestamp": "1789560000"}]
}


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    # Cache and host breakers are process-global by design (one run = one process), so tests
    # must reset both or a deliberately-failing test leaks into the next one.
    cache.clear()
    http.host_breakers.breakers.clear()
    yield
    cache.clear()
    http.host_breakers.breakers.clear()


@respx.mock
def test_okx_ticker_carries_provenance_and_an_asof(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        return_value=httpx.Response(200, json=OKX_TICKER)
    )
    obs = okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert obs.source == "okx.public.ticker"
    assert obs.tier == 1
    assert obs.quality == "stale"  # the 2026-09-16 fixture is older than the price TTL (P1-2)
    assert obs.asof == datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    age = obs.age(datetime(2026, 9, 16, 12, 5, tzinfo=UTC))
    assert age is not None and age.total_seconds() == 300


@respx.mock
def test_okx_candles_stamp_the_newest_bar() -> None:
    respx.get(url__regex=r".*/api/v5/market/candles").mock(
        return_value=httpx.Response(200, json=OKX_CANDLES)
    )
    obs = okx_public.fetch_candles("BTC-USDT-SWAP", "1H", 2)
    # D-18c: each bar is its own series; one source name for all three hid their ages.
    assert obs.source == "okx.public.candles.1H"
    # The newest *closed* bar opened 11:00 and closed 12:00; that close is what every indicator
    # (and the dossier's price) reads, so it is the data time.
    assert obs.asof == datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    assert isinstance(obs.data, dict) and len(obs.data["data"]) == 2


@respx.mock
def test_candle_asof_is_the_last_closed_bar_not_the_forming_one() -> None:
    """D-N4: a gap before the forming bar must not make the closed price look fresh."""
    forming = ["1789560000000", "75400", "75500", "75300", "75436.2", "1", "1", "0", "0"]
    # Closed bar 04:00-08:00; the 08:00-12:00 bar is missing (suspension, API gap).
    closed = ["1789531200000", "75200", "75450", "75100", "75400", "1", "1", "0", "1"]
    respx.get(url__regex=r".*/api/v5/market/candles").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [forming, closed]})
    )
    obs = okx_public.fetch_candles("BTC-USDT-SWAP", "4H", 2)
    assert obs.asof == datetime(2026, 9, 16, 8, 0, tzinfo=UTC)


@respx.mock
def test_candle_asof_when_the_newest_row_is_already_closed() -> None:
    rows = [
        ["1789560000000", "75400", "75500", "75300", "75436.2", "1", "1", "0", "1"],
        ["1789556400000", "75200", "75450", "75100", "75400", "1", "1", "0", "1"],
    ]
    respx.get(url__regex=r".*/api/v5/market/candles").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": rows})
    )
    obs = okx_public.fetch_candles("BTC-USDT-SWAP", "1H", 2)
    assert obs.asof == datetime(2026, 9, 16, 13, 0, tzinfo=UTC)


@respx.mock
def test_candles_with_only_a_forming_bar_have_no_asof() -> None:
    forming = ["1789560000000", "75400", "75500", "75300", "75436.2", "1", "1", "0", "0"]
    respx.get(url__regex=r".*/api/v5/market/candles").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [forming]})
    )
    assert okx_public.fetch_candles("BTC-USDT-SWAP", "1H", 1).asof is None


@respx.mock
def test_okx_funding_and_open_interest_are_derivatives_family() -> None:
    respx.get(url__regex=r".*/public/funding-rate").mock(
        return_value=httpx.Response(200, json=OKX_FUNDING)
    )
    respx.get(url__regex=r".*/public/open-interest").mock(
        return_value=httpx.Response(200, json=OKX_OI)
    )
    funding = okx_public.fetch_funding("BTC-USDT-SWAP")
    oi = okx_public.fetch_open_interest("BTC-USDT-SWAP")
    assert funding.asof is not None and oi.asof is not None
    assert isinstance(funding.data, dict)
    assert funding.data["data"][0]["fundingRate"] == "0.0000812"


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"code": "51001", "msg": "Instrument ID does not exist", "data": []}, id="51001"
        ),
        pytest.param({"code": "50011", "msg": "Too Many Requests", "data": []}, id="rate limit"),
        pytest.param({"msg": "", "data": []}, id="no code field"),
        pytest.param(["not", "an", "envelope"], id="not a dict"),
    ],
)
def test_an_okx_business_error_is_missing_and_never_cached(body: Any) -> None:
    """D-9: OKX answers HTTP 200 with `code != "0"` for business errors; that payload used to
    be cached as quality "ok" (24 h for the instrument list)."""
    route = respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        return_value=httpx.Response(200, json=body)
    )
    first = okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert first.quality == "missing"
    assert any("OKX" in n for n in first.notes)
    second = okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert second.quality == "missing"
    assert route.call_count == 2  # not served from cache
    # The host answered: a business error is not a host outage, so it must not open the
    # breaker that would skip every other OKX family for the rest of the run.
    okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert route.call_count == 3


@respx.mock
def test_a_non_okx_payload_is_not_checked_for_an_okx_code() -> None:
    respx.get(url__regex=r".*alternative\.me.*").mock(return_value=httpx.Response(200, json=FNG))
    assert fear_greed.fetch_fear_greed(1).quality == "ok"


@respx.mock
def test_a_dead_source_degrades_to_missing_instead_of_raising() -> None:
    respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        return_value=httpx.Response(503, text="maintenance")
    )
    obs = okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert obs.quality == "missing"
    assert obs.data is None
    assert "fetch failed" in obs.notes[0]


@respx.mock
def test_a_timeout_degrades_too_after_retrying() -> None:
    route = respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )
    obs = okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert obs.quality == "missing"
    assert route.call_count == 3  # retried with jitter, then gave up


@respx.mock
def test_the_second_call_in_a_run_is_served_from_cache() -> None:
    route = respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        return_value=httpx.Response(200, json=OKX_TICKER)
    )
    first = okx_public.fetch_ticker("BTC-USDT-SWAP")
    second = okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert route.call_count == 1  # one network call for two asks
    assert "from cache" in second.notes and "from cache" not in first.notes


@respx.mock
def test_a_cache_hit_reports_when_the_data_was_actually_fetched() -> None:
    """D-7: a hit was stamped `observed_at=now`, so a 6 h-old cached macro payload read as
    fetched this second."""
    respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        return_value=httpx.Response(200, json=OKX_TICKER)
    )
    first = okx_public.fetch_ticker("BTC-USDT-SWAP")
    second = okx_public.fetch_ticker("BTC-USDT-SWAP")
    assert "from cache" in second.notes
    assert second.observed_at == first.observed_at


def test_a_cache_entry_from_before_fetch_times_were_stored_is_refetched() -> None:
    store = cache.get_cache()
    store.set(cache.cache_key("legacy", {"x": 1}), {"old": "payload"})
    value, was_cached, _ = cache.cached("legacy", {"x": 1}, "price", lambda: {"new": "payload"})
    assert value == {"new": "payload"} and not was_cached


@respx.mock
def test_different_symbols_do_not_share_a_cache_entry() -> None:
    route = respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        return_value=httpx.Response(200, json=OKX_TICKER)
    )
    okx_public.fetch_ticker("BTC-USDT-SWAP")
    okx_public.fetch_ticker("ETH-USDT-SWAP")
    assert route.call_count == 2


@respx.mock
def test_rubik_positioning_series_are_fetched_per_currency() -> None:
    respx.get(url__regex=r".*/rubik/stat/contracts/long-short-account-ratio").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": [["1789560000000", "1.24"]]})
    )
    obs = okx_rubik.fetch_long_short_ratio("BTC")
    assert obs.source == "okx.rubik.long_short_account_ratio"
    assert obs.tier == 1
    # D-7: rubik series carried no data time; the newest bucket is it.
    assert obs.asof == datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@respx.mock
def test_funding_history_is_stamped_with_the_newest_settlement() -> None:
    rows = [
        {"fundingRate": "0.0001", "fundingTime": "1789560000000"},
        {"fundingRate": "0.0002", "fundingTime": "1789531200000"},
    ]
    respx.get(url__regex=r".*/public/funding-rate-history").mock(
        return_value=httpx.Response(200, json={"code": "0", "data": rows})
    )
    obs = okx_public.fetch_funding_history("BTC-USDT-SWAP")
    assert obs.asof == datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@respx.mock
def test_bybit_kz_uses_the_configured_regional_host() -> None:
    route = respx.get(url__regex=r"https://api\.bybit\.kz/v5/market/tickers").mock(
        return_value=httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
    )
    obs = bybit_kz_public.fetch_ticker("BTCUSDT")
    assert route.called
    assert obs.source == "bybit.kz.public.ticker"


@respx.mock
def test_binance_positioning_endpoints_are_reachable_through_the_envelope() -> None:
    respx.get(url__regex=r".*/futures/data/topLongShortPositionRatio").mock(
        return_value=httpx.Response(200, json=[{"symbol": "BTCUSDT", "longShortRatio": "1.8"}])
    )
    obs = binance_futures_public.fetch_top_trader_position_ratio("BTCUSDT")
    assert obs.quality == "ok"
    assert isinstance(obs.data, list)
    assert obs.data[0]["longShortRatio"] == "1.8"


@respx.mock
def test_hyperliquid_context_is_extracted_by_coin_name() -> None:
    payload = [
        {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
        [{"funding": "0.0000125", "openInterest": "5000"}, {"funding": "-0.00002"}],
    ]
    respx.post("https://api.hyperliquid.xyz/info").mock(
        return_value=httpx.Response(200, json=payload)
    )
    obs = hyperliquid.fetch_meta_and_contexts()
    btc = hyperliquid.asset_context(obs, "BTC")
    eth = hyperliquid.asset_context(obs, "eth")  # case-insensitive
    assert btc is not None and btc["funding"] == "0.0000125"
    assert eth is not None and eth["funding"] == "-0.00002"
    assert hyperliquid.asset_context(obs, "DOGE") is None


@respx.mock
def test_deribit_dvol_is_a_derivatives_observation() -> None:
    respx.get(url__regex=r".*/get_volatility_index_data").mock(
        return_value=httpx.Response(200, json={"result": {"data": [[1789560000000, 45.2]]}})
    )
    obs = deribit.fetch_dvol("BTC")
    assert obs.source == "deribit.dvol"
    assert obs.quality == "ok"


@respx.mock
def test_fear_greed_stamps_asof_from_the_newest_reading() -> None:
    respx.get(url__regex=r"https://api\.alternative\.me/fng/").mock(
        return_value=httpx.Response(200, json=FNG)
    )
    obs = fear_greed.fetch_fear_greed(30)
    assert obs.asof == datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def test_key_gated_fetchers_report_missing_rather_than_calling_without_a_key() -> None:
    from argus.data.fetchers import cmc, coinalyze

    quotes = cmc.fetch_quotes("BTC")
    assert quotes.quality == "missing" and "CMC_API_KEY" in quotes.notes[0]
    series = coinalyze.fetch_open_interest_history("BTCUSDT_PERP.A", "1hour", 0, 1)
    assert series.quality == "missing" and "COINALYZE_API_KEY" in series.notes[0]


def test_stale_marking_is_available_on_every_observation() -> None:
    from datetime import timedelta

    obs = Observation(source="x", tier=1, asof=datetime(2026, 9, 16, 6, tzinfo=UTC))
    marked = obs.mark_stale(timedelta(hours=2), now=datetime(2026, 9, 16, 12, tzinfo=UTC))
    assert marked.quality == "stale"


@respx.mock
def test_a_blocked_host_is_skipped_after_two_failures_instead_of_retried_all_run() -> None:
    # Measured 2026-09-16: five geo-blocked OKX sources burned ~5 min of an 8 min run budget.
    route = respx.get(url__regex=r".*/api/v5/.*").mock(side_effect=httpx.ConnectTimeout("blocked"))
    first = okx_public.fetch_ticker("BTC-USDT-SWAP")
    second = okx_public.fetch_funding("BTC-USDT-SWAP")
    third = okx_public.fetch_open_interest("BTC-USDT-SWAP")
    assert first.quality == second.quality == third.quality == "missing"
    assert route.call_count == 6  # 3 retries each for two calls, then the breaker opens
    assert "breaker open" in third.notes[0]


@respx.mock
def test_a_404_on_one_slug_does_not_open_the_breaker_for_every_asset() -> None:
    """D-18f: two unknown DefiLlama slugs used to open the `api.llama.fi` breaker, so every
    other asset lost its onchain family for the rest of the run."""
    respx.get(url__regex=r".*api\.llama\.fi/tvl/(nope|gone)$").mock(
        return_value=httpx.Response(404, text="protocol not found")
    )
    good = respx.get(url__regex=r".*api\.llama\.fi/tvl/aave$").mock(
        return_value=httpx.Response(200, json=12.5)
    )
    assert defillama.fetch_protocol_tvl("nope").quality == "missing"
    assert defillama.fetch_protocol_tvl("gone").quality == "missing"
    assert defillama.fetch_protocol_tvl("aave").quality == "ok"
    assert good.call_count == 1


@respx.mock
def test_a_cache_hit_does_not_reset_the_hosts_failure_count() -> None:
    """D-18f: a cache hit proves nothing about the host. Counting it as a success cleared the
    failure window, so a blocked host interleaved with cached reads never tripped (F-040)."""
    respx.get(url__regex=r".*/api/v5/market/ticker").mock(
        return_value=httpx.Response(200, json=OKX_TICKER)
    )
    down = respx.get(url__regex=r".*/api/v5/public/.*").mock(
        side_effect=httpx.ConnectTimeout("blocked")
    )
    okx_public.fetch_ticker("BTC-USDT-SWAP")  # real success, now cached
    okx_public.fetch_funding("BTC-USDT-SWAP")  # failure 1
    okx_public.fetch_ticker("BTC-USDT-SWAP")  # cache hit
    okx_public.fetch_open_interest("BTC-USDT-SWAP")  # failure 2 -> breaker opens
    calls = down.call_count
    skipped = okx_public.fetch_funding_history("BTC-USDT-SWAP")
    assert down.call_count == calls
    assert "breaker open" in skipped.notes[0]


@respx.mock
def test_rubik_429s_do_not_open_the_breaker_for_candles_and_funding() -> None:
    """A rubik 429 counted against the www.okx.com breaker every OKX family shares: two of
    them skipped candles and funding for the next 60 s, so later assets lost their
    trade-critical families. Rubik failures now trip rubik's own breaker only."""
    throttled = respx.get(url__regex=r".*/api/v5/rubik/.*").mock(
        return_value=httpx.Response(429, json={"code": "50011", "msg": "Too Many Requests"})
    )
    respx.get(url__regex=r".*/api/v5/public/funding-rate").mock(
        return_value=httpx.Response(200, json=OKX_FUNDING)
    )
    assert okx_rubik.fetch_taker_volume("SOL").quality == "missing"
    assert okx_rubik.fetch_taker_volume("ARB").quality == "missing"
    assert (
        okx_public.fetch_funding("BTC-USDT-SWAP").quality != "missing"
    )  # fetched (fixture is old)
    # Rubik's own breaker is open: a third rubik call is skipped, not sent.
    calls = throttled.call_count
    skipped = okx_rubik.fetch_long_short_ratio("ETH")
    assert throttled.call_count == calls
    assert "breaker open" in skipped.notes[0]


@respx.mock
def test_an_oversized_response_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-18f: no cap meant one misbehaving provider could hold the whole body in memory."""
    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 1_000)
    respx.get(url__regex=r".*alternative\.me.*").mock(
        return_value=httpx.Response(200, json={"data": [{"value": "x" * 5_000}]})
    )
    obs = fear_greed.fetch_fear_greed(1)
    assert obs.quality == "missing"
    assert any("exceeds" in n for n in obs.notes)
