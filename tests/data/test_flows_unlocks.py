"""ETF flows (Farside via Jina) and token unlocks (DefiLlama dataset CDN). Recorded 2026-09-18."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from argus.data import cache, http
from argus.data.envelope import Observation
from argus.data.fetchers import farside, unlocks

# Farside renders "-" for a blank cell and "(95.1)" for an outflow; "0.0" is a real zero.
FARSIDE = """Title: Farside Investors

Bitcoin ETF Flow – All Data (US$m)
Date\t
IBIT
\tFBTC
\tGBTC
\tTotal

11 Jan 2024\t
111.7
\t227.0
\t(95.1)
\t243.6

15 Sep 2026\t
-
\t0.0
\t(12.5)
\t(12.5)

16 Sep 2026\t
50.0
\t10.0
\t-
\t60.0

17 Sep 2026\t
-
\t-
\t-
\t0.0
"""


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    cache.clear()
    http.host_breakers.breakers.clear()
    yield
    cache.clear()
    http.host_breakers.breakers.clear()


def _obs(text: str = FARSIDE) -> Observation:
    return Observation(source="farside.btc", tier=1, data={"text": text})


def test_a_dash_is_missing_and_zero_point_zero_is_a_real_zero() -> None:
    # FAILURE_LOG F-009: a blank cell read as 0 hid −$283M of outflows.
    rows = farside.parse_flows(_obs())
    row = next(r for r in rows if r["date"] == date(2026, 9, 15))
    assert row["issuers"]["IBIT"] is None
    assert row["issuers"]["FBTC"] == 0.0
    assert row["issuers"]["GBTC"] == -12.5


def test_parentheses_are_outflows() -> None:
    rows = farside.parse_flows(_obs())
    assert rows[0]["total"] == 243.6
    assert rows[0]["issuers"]["GBTC"] == -95.1


def test_a_row_of_blanks_with_a_zero_total_is_no_data_at_all() -> None:
    # Trap 18: "Total 0.0" under an all-blank row is a rendering default.
    rows = farside.parse_flows(_obs())
    row = next(r for r in rows if r["date"] == date(2026, 9, 17))
    assert row["total"] is None
    assert row["suspect"] is True


def test_net_flow_sums_only_days_that_reported() -> None:
    rows = farside.parse_flows(_obs())
    flow, days = farside.net_flow(rows, days=5, today=date(2026, 9, 18))
    assert flow == pytest.approx(-12.5 + 60.0)
    assert days == 2  # 17 Sep had no data and is not counted as a zero


def test_an_empty_cell_cannot_shift_values_into_the_wrong_column() -> None:
    """D-12: empty tokens were dropped before slicing, so one empty cell moved every later
    value one column left and swallowed the next row's date as the 'Total'."""
    shifted = FARSIDE.replace("16 Sep 2026\t\n50.0\n\t10.0\n", "16 Sep 2026\t\n50.0\n\t\n")
    rows = farside.parse_flows(_obs(shifted))
    row = next(r for r in rows if r["date"] == date(2026, 9, 16))
    assert row["total"] is None and row["suspect"] is True
    assert any(r["date"] == date(2026, 9, 17) for r in rows)  # the next row survived


def test_a_table_whose_last_column_is_not_total_is_not_read() -> None:
    no_total = FARSIDE.replace("\tTotal\n", "\tARKB\n")
    assert farside.parse_flows(_obs(no_total)) == []


def test_issuers_that_do_not_add_up_to_the_total_make_the_row_suspect() -> None:
    wrong = FARSIDE.replace("\t(95.1)\n\t243.6", "\t(95.1)\n\t943.6")
    rows = farside.parse_flows(_obs(wrong))
    assert rows[0]["total"] is None and rows[0]["suspect"] is True


def test_farside_is_tier_2_because_it_arrives_through_a_third_party_reader() -> None:
    assert farside.TIER == 2


def test_no_usable_rows_returns_none_rather_than_zero() -> None:
    flow, days = farside.net_flow([], days=5, today=date(2026, 9, 18))
    assert flow is None and days == 0


@respx.mock
def test_the_fetcher_reads_farside_through_the_jina_reader() -> None:
    route = respx.get(url__regex=r"https://r\.jina\.ai/.*farside.*").mock(
        return_value=httpx.Response(200, text=FARSIDE)
    )
    obs = farside.fetch_btc_flows()
    assert route.called
    assert obs.quality == "ok" and isinstance(obs.data, dict)


# --- unlocks --------------------------------------------------------------------------------

UNLOCK_PAYLOAD = {
    "documentedData": {
        "data": [
            {
                "label": "Investors",
                "data": [
                    {"timestamp": 1789603200, "unlocked": 1000.0},  # 2026-09-17
                    {"timestamp": 1789689600, "unlocked": 1000.0},  # 2026-09-18
                    {"timestamp": 1790294400, "unlocked": 1300.0},  # 2026-09-25 step
                ],
            },
            {
                "label": "Team",
                "data": [
                    {"timestamp": 1789603200, "unlocked": 500.0},
                    {"timestamp": 1789689600, "unlocked": 500.0},
                    {"timestamp": 1790294400, "unlocked": 500.0},
                ],
            },
        ]
    },
    "metadata": {
        "events": [{"description": "Cliff unlock for Investors", "timestamp": 1790294400}]
    },
    "supplyMetrics": {"maxSupply": 15000.0, "adjustedSupply": 13000.0},
}


def test_next_unlock_finds_the_next_step_and_sizes_it() -> None:
    now = datetime(2026, 9, 18, 12, tzinfo=UTC)
    event = unlocks.next_unlock(Observation(source="unlocks.x", tier=1, data=UNLOCK_PAYLOAD), now)
    assert event is not None
    assert event["date"] == date(2026, 9, 25)
    assert event["in_days"] == 7
    assert event["tokens"] == pytest.approx(300.0)
    # 300 new tokens against 1,500 already unlocked
    assert event["pct_of_unlocked"] == pytest.approx(20.0)
    assert "Investors" in event["recipients"]


def _day(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _schedule(*categories: tuple[str, list[tuple[date, float]]]) -> Observation:
    data = [
        {"label": label, "data": [{"timestamp": _day(d), "unlocked": v} for d, v in points]}
        for label, points in categories
    ]
    return Observation(source="unlocks.x", tier=1, data={"documentedData": {"data": data}})


def test_a_linear_emitter_does_not_hide_a_cliff_a_few_days_later() -> None:
    """D-14: the first step above dust was reported, so a daily emitter read "in 1 d, 0.08%"
    while a 4% investor cliff five days out never reached the dossier."""
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(60)]
    ecosystem = [(d, 10_000.0 + 8.0 * i) for i, d in enumerate(days)]
    investors = [(d, 0.0 if d < date(2026, 9, 23) else 400.0) for d in days]
    obs = _schedule(("Ecosystem", ecosystem), ("Investors", investors))
    event = unlocks.next_unlock(obs, datetime(2026, 9, 18, 12, tzinfo=UTC))
    assert event is not None
    assert event["date"] == date(2026, 9, 23) and event["in_days"] == 5
    assert "Investors" in event["recipients"]
    assert event["pct_of_unlocked"] > 3.9
    # The emission around it is still reported, as sums over the next 7 and 30 days.
    assert event["pct_next_7d"] > event["pct_of_unlocked"]
    assert event["pct_next_30d"] > event["pct_next_7d"]


def test_a_near_cliff_is_not_hidden_behind_a_larger_one_later() -> None:
    """The D-14 fix reported the largest step in 30 days, so a 2% cliff in 3 days vanished
    behind a 4% cliff in 25. The next material cliff leads; the largest is reported beside it."""
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(60)]
    base = [(d, 100e6) for d in days]
    investors = [(d, 0.0 if d < date(2026, 9, 21) else 2e6) for d in days]
    team = [(d, 0.0 if d < date(2026, 10, 13) else 4e6) for d in days]
    obs = _schedule(("Base", base), ("Investors", investors), ("Team", team))
    event = unlocks.next_unlock(obs, datetime(2026, 9, 18, 12, tzinfo=UTC))
    assert event is not None
    assert event["in_days"] == 3 and event["pct_of_unlocked"] == pytest.approx(2.0)
    assert event["recipients"] == ["Investors"]
    assert event["max_30d_in_days"] == 25
    assert event["max_30d_pct"] == pytest.approx(4.0)


def test_a_noisy_linear_emitter_reads_as_daily_emission_not_a_month_away() -> None:
    """The D-14 fix picked the largest step in 30 days; a daily step growing by one token made
    that the last day, so "emits every day" read as "nothing for a month"."""
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(60)]
    points, total = [], 100e6
    for i, d in enumerate(days):
        points.append((d, total))
        total += 100_000 + i
    event = unlocks.next_unlock(
        _schedule(("Ecosystem", points)), datetime(2026, 9, 18, 12, tzinfo=UTC)
    )
    assert event is not None
    assert event["in_days"] == 1 and event["pct_of_unlocked"] == pytest.approx(0.1, rel=0.05)
    assert event["pct_next_7d"] == pytest.approx(0.7, rel=0.05)


def test_a_sparse_category_does_not_create_a_false_cliff() -> None:
    """D-14: categories were summed only on the stamps where each had a point, so a category
    reported monthly vanished from the daily totals and reappeared as a fake unlock."""
    daily = [(date(2026, 9, 15) + timedelta(days=i), 1_000.0) for i in range(30)]
    monthly = [(date(2026, 9, 1), 5_000.0), (date(2026, 10, 1), 5_000.0)]
    obs = _schedule(("Ecosystem", daily), ("Treasury", monthly))
    assert unlocks.next_unlock(obs, datetime(2026, 9, 18, 12, tzinfo=UTC)) is None


def test_a_schedule_with_no_future_step_returns_none() -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    assert unlocks.next_unlock(Observation(source="u", tier=1, data=UNLOCK_PAYLOAD), now) is None


def test_a_missing_or_paid_payload_is_reported_not_guessed() -> None:
    missing = Observation.missing("unlocks.x", 1, "HTTP 402 paid-only")
    assert unlocks.next_unlock(missing, datetime(2026, 9, 18, tzinfo=UTC)) is None


@respx.mock
def test_the_unlock_fetcher_uses_the_free_dataset_host() -> None:
    # api.llama.fi/emissions is paid (F-048); the dataset CDN serves the same data free.
    route = respx.get("https://defillama-datasets.llama.fi/emissions/ethena").mock(
        return_value=httpx.Response(200, json=UNLOCK_PAYLOAD)
    )
    obs = unlocks.fetch_schedule("ethena")
    assert route.called and obs.quality == "ok"


def test_slug_lookup_prefers_an_exact_match_then_a_prefix() -> None:
    catalogue = ["arbitrum-foundation", "arbitrum-exchange", "ethena", "sui-foundation"]
    assert unlocks.match_slug("ethena", catalogue) == "ethena"
    assert unlocks.match_slug("sui", catalogue) == "sui-foundation"  # single prefix match
    # Ambiguous: arbitrum-foundation vs arbitrum-exchange — refuse, don't guess a schedule.
    assert unlocks.match_slug("arbitrum", catalogue) is None
    assert unlocks.match_slug("dogecoin", catalogue) is None
