"""One test per data-quality trap (DATA_SOURCES.md §9).

Each of these is a failure that was observed against free sources in September, not a
hypothetical. The comment on each test says what it cost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from argus.data import quality as q
from argus.data.quality import DataQualityEvent

NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)


def test_trap_01_aggregator_tables_are_not_a_price_source() -> None:
    with pytest.raises(DataQualityEvent, match="not a price source"):
        q.check_price_source_precedence("coingecko.homepage", "BTC")
    q.check_price_source_precedence("coingecko.coin_page", "BTC")  # coin pages are fine


def test_trap_02_price_outside_its_own_24h_range_is_stale() -> None:
    q.check_price_within_24h_range(Decimal("100"), Decimal("95"), Decimal("105"), "okx")
    with pytest.raises(DataQualityEvent, match="outside 24h range"):
        q.check_price_within_24h_range(Decimal("110"), Decimal("95"), Decimal("105"), "cmc")


def test_trap_03_blank_is_missing_never_zero() -> None:
    # F-009: a blank Farside cell read as 0 hid −$283M of ETF outflows.
    for blank in (None, "", "  ", "-", "—", "n/a"):
        with pytest.raises(DataQualityEvent, match="never as 0"):
            q.check_blank_not_zero(blank, "farside.btc", "flow_usd")
    q.check_blank_not_zero("0", "farside.btc", "flow_usd")  # an explicit zero is data


def test_trap_04_a_percentage_without_a_sign_cannot_be_trusted() -> None:
    q.check_sign_preserved("-3.4%", Decimal("-3.4"), "cmc")
    with pytest.raises(DataQualityEvent, match="no sign"):
        q.check_sign_preserved("3.4%", Decimal("3.4"), "cmc")


def test_trap_05_subscript_zero_notation_is_not_flattened() -> None:
    # The subscript counts zeros after the point: "$0.0₅3632" is 0.000003632, not 0.053632.
    assert q.parse_subscript_price("$0.0₅3632") == Decimal("0.000003632")
    assert q.parse_subscript_price("$0.0₃45") == Decimal("0.00045")
    assert q.parse_subscript_price("1.25") == Decimal("1.25")  # ordinary prices pass through
    with pytest.raises(DataQualityEvent, match="parsed as"):
        q.check_subscript_zero("$0.0₅3632", Decimal("0.053632"), "aggregator")


def test_trap_06_impossible_volume_discredits_the_row() -> None:
    q.check_volume_plausible(Decimal("1e8"), Decimal("1e9"), "coingecko")
    with pytest.raises(DataQualityEvent, match="discard the row"):
        q.check_volume_plausible(Decimal("342e9"), Decimal("2e9"), "aggregator", "ARB")
    with pytest.raises(DataQualityEvent, match="negative volume"):
        q.check_volume_plausible(Decimal("-1"), None, "aggregator")


def test_trap_07_a_value_without_a_timestamp_is_not_evidence() -> None:
    q.check_timestamped_api_not_web("coinalyze.api", has_timestamp=True)
    with pytest.raises(DataQualityEvent, match="no timestamp"):
        q.check_timestamped_api_not_web("coinalyze.web", has_timestamp=False)


def test_trap_08_aggregate_liquidation_totals_are_soft() -> None:
    q.check_liquidation_source("coinalyze.per_asset", per_asset=True)
    with pytest.raises(DataQualityEvent, match="soft evidence"):
        q.check_liquidation_source("news.aggregate", per_asset=False)


def test_trap_09_one_vendor_per_series() -> None:
    # F-022: mixing vendors in one OI series manufactured a false "OI collapse".
    q.check_single_vendor_series(["coinalyze", "coinalyze"], "open_interest")
    with pytest.raises(DataQualityEvent, match="mixes vendors"):
        q.check_single_vendor_series(["coinalyze", "binance"], "open_interest", "BTC")


def test_trap_10_stale_articles_cannot_evidence_current_claims() -> None:
    # F-012: ~1/3 of on-chain search hits were 6–12 months old, presented as current.
    q.check_article_recency(NOW - timedelta(days=2), "google_news", now=NOW)
    with pytest.raises(DataQualityEvent, match="too old"):
        q.check_article_recency(NOW - timedelta(days=200), "google_news", now=NOW)
    with pytest.raises(DataQualityEvent, match="no publication date"):
        q.check_article_recency(None, "scrape", now=NOW)


def test_trap_11_unmapped_symbols_are_refused() -> None:
    # TON became GRAM; a stale mapping silently fetches the wrong asset.
    known = {"BTC": "bitcoin", "GRAM": "the-open-network"}
    q.check_symbol_mapping("GRAM", known, "universe.yaml")
    with pytest.raises(DataQualityEvent, match="no verified mapping"):
        q.check_symbol_mapping("TON", known, "universe.yaml")


def test_trap_12_catalyst_dates_need_a_primary_source() -> None:
    q.check_catalyst_primary_source("https://sec.gov/filing/123", "catalysts.yaml")
    with pytest.raises(DataQualityEvent, match="primary source"):
        q.check_catalyst_primary_source("https://coinmarketcal.com/en/event/x", "catalysts.yaml")
    with pytest.raises(DataQualityEvent, match="primary source"):
        q.check_catalyst_primary_source(None, "catalysts.yaml")


def test_trap_13_notional_is_not_loss() -> None:
    with pytest.raises(DataQualityEvent, match="notional headline"):
        q.check_notional_not_loss("loss", "$1.2B liquidated in 24 hours", "cryptopanic")
    q.check_notional_not_loss("loss", "Fund reports $40M realised loss", "cryptopanic")


def test_trap_14_wallet_attribution_needs_corroboration() -> None:
    q.check_wallet_attribution("Alameda", ["https://a", "https://b"], "lookonchain")
    with pytest.raises(DataQualityEvent, match="rests on 1 source"):
        q.check_wallet_attribution("Alameda", ["https://a"], "lookonchain")


def test_trap_15_a_fresh_timestamp_does_not_mean_fresh_values() -> None:
    # F-011: loris.tools' ETH page carried a current stamp with values 6% off.
    q.check_cross_source_price(Decimal("2400"), Decimal("2405"), "loris")
    with pytest.raises(DataQualityEvent, match="distrust the whole page"):
        q.check_cross_source_price(Decimal("2260"), Decimal("2400"), "loris", symbol="ETH")


def test_trap_16_macro_prints_need_two_sources() -> None:
    q.check_two_sources_for_print(["fred", "investing"], "CPI")
    with pytest.raises(DataQualityEvent, match="single source"):
        q.check_two_sources_for_print(["fred"], "CPI")


def test_trap_17_price_consensus_needs_three_agreeing_sources() -> None:
    # F-010: BTC quoted ±2% apart, in opposite directions, across aggregators.
    agreeing = {
        "okx": Decimal("75000"),
        "binance": Decimal("75010"),
        "bybit": Decimal("74990"),
        "cmc": Decimal("75005"),
    }
    q.check_price_consensus(agreeing, "BTC")
    with pytest.raises(DataQualityEvent, match="needs >=3 sources"):
        q.check_price_consensus({"okx": Decimal("75000")}, "BTC")
    scattered = {
        "okx": Decimal("75000"),
        "cmc": Decimal("73500"),
        "coingecko": Decimal("76500"),
    }
    with pytest.raises(DataQualityEvent, match="sources agree"):
        q.check_price_consensus(scattered, "BTC")


def test_trap_18_a_total_under_blank_rows_is_a_render_default() -> None:
    with pytest.raises(DataQualityEvent, match="no data"):
        q.check_total_row_not_zero(["", "", None], total="0.0", source="farside")
    q.check_total_row_not_zero(["-283.4", "", None], total="0.0", source="farside")


def test_collect_gathers_every_event_instead_of_stopping_at_the_first() -> None:
    events = q.collect(
        lambda: q.check_blank_not_zero(None, "farside", "flow"),
        lambda: q.check_two_sources_for_print(["fred"], "CPI"),
        lambda: q.check_liquidation_source("coinalyze", per_asset=True),  # passes
    )
    assert [e.trap for e in events] == ["trap_03_blank_not_zero", "trap_16_single_source_print"]
    assert [e.severity for e in events] == ["reject", "suspect"]


def test_unparseable_subscript_text_is_a_quality_event_not_a_crash() -> None:
    # antigravity review M2, reproduced: InvalidOperation escaped collect() and would have
    # aborted the whole quality pass for one garbage cell.
    events = q.collect(lambda: q.check_subscript_zero("$0.0₅N/A", Decimal("0"), "aggregator"))
    assert [e.trap for e in events] == ["trap_05_subscript_zero"]
    assert events[0].severity == "reject"
    with pytest.raises(ValueError, match="unparseable"):
        q.parse_subscript_price("N/A")
