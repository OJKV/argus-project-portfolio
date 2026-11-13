"""Live assembly (`features.market`) against fake fetchers — no network (D-17).

Each fetcher the module calls is replaced at its module attribute, so the wiring between a
payload and the number that reaches the regime view, the dossier or the sizing spec is what is
under test, not the fetchers (those have their own tests in `tests/data/`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from argus.data.envelope import Observation
from argus.data.fetchers import (
    defillama,
    farside,
    fear_greed,
    forexfactory,
    fred,
    okx_public,
    okx_rubik,
    unlocks,
)
from argus.features import market
from argus.features.universe import Asset

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)  # a Wednesday
BTC = Asset(symbol="BTC", okx="BTC-USDT-SWAP", coingecko="bitcoin", source="core")
ETH = Asset(symbol="ETH", okx="ETH-USDT-SWAP", coingecko="ethereum", source="core")
SOL = Asset(symbol="SOL", okx="SOL-USDT-SWAP", coingecko="solana", source="core")


def _obs(source: str, data: Any, quality: str = "ok") -> Observation:
    return Observation.model_validate(
        {"source": source, "tier": 1, "quality": quality, "data": data}
    )


def candle_rows(n: int, bar_hours: float, price: float = 100.0, step: float = 0.5) -> list[Any]:
    """`n` OKX candle rows, newest first; the newest is the forming bar (confirm "0")."""
    rows: list[Any] = []
    for i in range(n):
        ts = int((NOW - timedelta(hours=bar_hours * i)).timestamp() * 1000)
        close = price + step * (n - i)
        rows.append(
            [str(ts), str(close), str(close + 1), str(close - 1), str(close)]
            + ["10", "10", "1000", "0" if i == 0 else "1"]
        )
    return rows


def ff_row(title: str, at: str, impact: str = "High", country: str = "USD") -> dict[str, Any]:
    return {
        "title": title,
        "country": country,
        "date": at,
        "impact": impact,
        "forecast": "",
        "previous": "0.2%",
    }


# The week of NOW (Sun 4 Oct - Sat 10 Oct, US Eastern offsets as the export carries them).
THIS_WEEK = [
    ff_row("Bank Holiday", "2026-10-05T00:00:00-04:00", "Holiday", "NZD"),
    ff_row("German ZEW", "2026-10-06T05:00:00-04:00", "Medium", "EUR"),
    ff_row("FOMC Minutes", "2026-10-06T14:00:00-04:00"),
    ff_row("CPI m/m", "2026-10-08T08:30:00-04:00"),
    ff_row("Retail Sales", "2026-10-09T08:30:00-04:00", "Low"),
]


class Fakes:
    """Payloads the fake fetchers serve; a test overrides one field and calls the code."""

    def __init__(self) -> None:
        self.candles: dict[tuple[str, str], list[Any]] = {}
        self.funding: dict[str, Any] = {"fundingRate": "0.0001", "ts": "1791374400000"}
        self.fng: Any = {"data": [{"value": "61", "timestamp": "1791331200"}]}
        self.calendar: Any = list(THIS_WEEK)
        self.calendar_quality = "ok"

    def fetch_candles(self, inst_id: str, bar: str = "1H", limit: int = 300) -> Observation:
        hours = {"1D": 24.0, "4H": 4.0, "1H": 1.0}[bar]
        rows = self.candles.get((inst_id, bar), candle_rows(min(limit, 300), hours))
        return _obs(f"okx.public.candles.{bar}", {"code": "0", "data": rows})

    def fetch_funding(self, inst_id: str) -> Observation:
        return _obs("okx.public.funding", {"code": "0", "data": [self.funding]})

    def fetch_calendar(self, *args: Any, **kwargs: Any) -> Observation:
        return _obs("forexfactory.calendar", self.calendar, self.calendar_quality)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    fake = Fakes()
    monkeypatch.setattr(okx_public, "fetch_candles", fake.fetch_candles)
    monkeypatch.setattr(okx_public, "fetch_funding", fake.fetch_funding)
    monkeypatch.setattr(
        okx_rubik,
        "fetch_open_interest_volume",
        lambda *a, **k: _obs("okx.rubik.oi", {"code": "0", "data": []}),
    )
    monkeypatch.setattr(
        defillama, "fetch_stablecoin_supply", lambda *a, **k: _obs("defillama.stables", [])
    )
    monkeypatch.setattr(
        farside, "fetch_btc_flows", lambda *a, **k: Observation.missing("farside", 1, "off")
    )
    monkeypatch.setattr(
        fred, "fetch_series", lambda *a, **k: Observation.missing("fred", 1, "no key")
    )
    monkeypatch.setattr(
        fear_greed, "fetch_fear_greed", lambda *a, **k: _obs("alternative.fng", fake.fng)
    )
    # The calendar is faked one layer down so its validation (D-4) runs for real.
    monkeypatch.setattr(forexfactory, "fetch_json", fake.fetch_calendar)
    return fake


def regime(fakes: Fakes) -> Any:
    view, _ = market.regime_snapshot([BTC, ETH, SOL], now=NOW)
    return view.inputs


# --- regime snapshot ---------------------------------------------------------------------


def test_regime_snapshot_reads_every_input_it_was_given(fakes: Fakes) -> None:
    inputs = regime(fakes)
    assert inputs.btc_close is not None and inputs.btc_ema20 is not None
    assert inputs.breadth_20 == 100.0  # every fake series rises
    assert inputs.funding_median == pytest.approx(0.0001)
    assert inputs.fear_greed == 61.0
    assert inputs.next_event == "CPI m/m"
    assert inputs.next_event_hours == pytest.approx(24.5)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"value": "n/a"}]},
        {"data": [{"value_classification": "Greed"}]},
        {"data": [{"value": None}]},
        {"data": ["61"]},
    ],
)
def test_a_malformed_fear_greed_reading_is_a_gap_not_an_abort(fakes: Fakes, payload: Any) -> None:
    """D-N6 / D-18g: one bad FNG row used to raise out of `regime_snapshot` and lose the
    whole regime (calendar, trend, vol) for the run."""
    fakes.fng = payload
    inputs = regime(fakes)
    assert inputs.fear_greed is None
    assert inputs.next_event == "CPI m/m"  # the rest of the snapshot survived


@pytest.mark.parametrize("rate", ["abc", None, "", {"x": 1}])
def test_a_malformed_funding_row_is_skipped(fakes: Fakes, rate: Any) -> None:
    fakes.funding = {"fundingRate": rate}
    inputs = regime(fakes)
    assert inputs.funding_median is None
    assert inputs.next_event == "CPI m/m"


# --- event calendar (D-4: fail closed) ---------------------------------------------------


def test_impact_label_case_drift_still_finds_the_event(fakes: Fakes) -> None:
    fakes.calendar = [r | {"impact": str(r["impact"]).lower()} for r in THIS_WEEK]
    inputs = regime(fakes)
    assert inputs.next_event == "CPI m/m"
    assert inputs.next_event_hours == pytest.approx(24.5)


@pytest.mark.parametrize(
    "calendar",
    [
        pytest.param([], id="empty export"),
        pytest.param({"error": "throttled"}, id="not a list"),
        pytest.param([r | {"impact": "Red"} for r in THIS_WEEK], id="unknown impact vocabulary"),
        pytest.param(
            [r | {"date": r["date"].replace("2026-10-0", "2026-09-2")} for r in THIS_WEEK],
            id="last week's export",
        ),
        pytest.param([r | {"date": "Thu 8:30am"} for r in THIS_WEEK], id="date format drift"),
    ],
)
def test_an_unusable_calendar_is_unknown_not_a_quiet_week(fakes: Fakes, calendar: Any) -> None:
    """D-4: these all used to land in the 168 h "none left this week" branch — TREND at full
    size and gate 8's blackout off. Unknown must stay unknown (regime maps it to CHOP)."""
    fakes.calendar = calendar
    view, prov = market.regime_snapshot([BTC, ETH, SOL], now=NOW)
    assert view.inputs is not None and view.inputs.next_event_hours is None
    assert view.label != "TREND_UP"
    cal = next(o for o in prov if o.source == "forexfactory.calendar")
    assert cal.quality != "ok"


DOGE = Asset(symbol="DOGE", okx="DOGE-USDT-SWAP", coingecko="dogecoin", source="core")
DOGE_ROW = {
    "instId": "DOGE-USDT-SWAP",
    "ctVal": "1000",
    "ctMult": "1",
    "lotSz": "0.01",
    "minSz": "0.01",
    "lever": "50",
    "state": "live",
}


def _serve_instruments(monkeypatch: pytest.MonkeyPatch, rows: list[Any]) -> None:
    monkeypatch.setattr(
        market, "fetch_json", lambda *a, **k: _obs("okx.public.instrument", {"data": rows})
    )


def test_instrument_spec_reads_the_okx_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_instruments(monkeypatch, [DOGE_ROW])
    spec = market.instrument_spec(DOGE)
    assert spec.contract_value == Decimal("1000")
    assert spec.lot_size == Decimal("0.01") and spec.min_contracts == Decimal("0.01")
    assert spec.max_leverage == 50


def test_instrument_spec_applies_the_contract_multiplier(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_instruments(monkeypatch, [DOGE_ROW | {"ctVal": "100", "ctMult": "10"}])
    assert market.instrument_spec(DOGE).contract_value == Decimal("1000")


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"ctVal": ""}, id="ctVal empty"),
        pytest.param({"ctVal": None}, id="ctVal null"),
        pytest.param({"ctVal": "0"}, id="ctVal zero"),
        pytest.param({"ctMult": ""}, id="ctMult empty"),
        pytest.param({"lotSz": "-1"}, id="lotSz negative"),
        pytest.param({"minSz": "NaN"}, id="minSz NaN"),
        pytest.param({"lever": ""}, id="lever empty"),
        pytest.param({"lever": "0"}, id="lever zero"),
        pytest.param({"state": "suspend"}, id="suspended"),
        pytest.param({"state": None}, id="state missing"),
        pytest.param({"instId": "BTC-USDT-SWAP"}, id="another instrument's row"),
    ],
)
def test_instrument_spec_refuses_to_guess(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, Any]
) -> None:
    """D-5: `ctVal or 1` turned a blank DOGE field (ctVal=1000) into a 1000x oversized order,
    cached for 24 h. A spec that cannot be read exactly must stop the asset, not default."""
    _serve_instruments(monkeypatch, [DOGE_ROW | override])
    with pytest.raises(ValueError):
        market.instrument_spec(DOGE)


def test_a_valid_week_with_nothing_left_still_reads_as_none_left(fakes: Fakes) -> None:
    fakes.calendar = [r for r in THIS_WEEK if r["title"] != "CPI m/m"]
    inputs = regime(fakes)
    assert inputs.next_event == "none left this week"
    assert inputs.next_event_hours == 168.0


# --- technical family --------------------------------------------------------------------


def test_technical_prices_from_the_last_closed_4h_bar_not_the_forming_one(fakes: Fakes) -> None:
    out, prov = market.technical(SOL, btc_daily=pd.DataFrame())
    # 300 rows: the forming bar closes at 250.0, the newest closed one at 249.5.
    assert out["price"] == 249.5
    assert out["ema_4h"] is not None and out["atr_4h"] > 0
    assert out["beta_btc_30d"] is None  # no BTC frame, no beta - not a 0
    assert [o.source for o in prov] == [f"okx.public.candles.{b}" for b in ("1D", "4H", "1H")]


def test_technical_beta_uses_the_btc_frame_it_is_given(fakes: Fakes) -> None:
    btc = market.indicators.candles_to_frame(candle_rows(300, 24.0))
    out, _ = market.technical(SOL, btc_daily=btc)
    assert out["beta_btc_30d"] == pytest.approx(1.0)


def test_a_dead_candle_feed_is_a_gap_not_a_price(fakes: Fakes) -> None:
    fakes.candles[("SOL-USDT-SWAP", "4H")] = []
    out, _ = market.technical(SOL, btc_daily=pd.DataFrame())
    assert "price" not in out and "atr_4h" not in out
    assert out["ema_1d"] is not None  # the other timeframes still arrive


# --- derivatives family ------------------------------------------------------------------


def _rubik(rows: list[list[str]]) -> Observation:
    return _obs("okx.rubik", {"code": "0", "data": rows})


@pytest.fixture
def venues(monkeypatch: pytest.MonkeyPatch, fakes: Fakes) -> dict[str, Any]:
    served: dict[str, Any] = {
        "oi": [{"oiCcy": "2850"}],
        # 90 settlements, newest first: 21 at 0.0003 then 69 at 0.0001.
        "hist": [{"fundingRate": "0.0003"}] * 21 + [{"fundingRate": "0.0001"}] * 69,
        "oi_1h": [[str(i), str(1000 + (24 - i) * 10), "0"] for i in range(25)],
        "oi_1d": [[str(i), str(2000 - i * 100), "0"] for i in range(8)],
        "ls": [["1", "1.24"]],
        "taker": [["1", "100", "150"]] * 24,
    }
    monkeypatch.setattr(
        okx_public, "fetch_open_interest", lambda *a: _obs("oi", {"data": served["oi"]})
    )
    monkeypatch.setattr(
        okx_public, "fetch_funding_history", lambda *a: _obs("h", {"data": served["hist"]})
    )
    monkeypatch.setattr(
        okx_rubik,
        "fetch_open_interest_volume",
        lambda ccy, period="1H": _rubik(served["oi_1h" if period == "1H" else "oi_1d"]),
    )
    monkeypatch.setattr(okx_rubik, "fetch_long_short_ratio", lambda *a: _rubik(served["ls"]))
    monkeypatch.setattr(okx_rubik, "fetch_taker_volume", lambda *a: _rubik(served["taker"]))
    fakes.funding = {"fundingRate": "0.0002", "nextFundingRate": "0.00025"}
    return served


HL = _obs("hyperliquid", [{"universe": [{"name": "SOL"}]}, [{"funding": "0.0000125"}]])


def test_derivatives_maps_every_venue_field(venues: dict[str, Any]) -> None:
    out, prov, history = market.derivatives(SOL, HL)
    assert out["funding_8h"] == pytest.approx(0.0002)
    assert out["next_funding_8h"] == pytest.approx(0.00025)
    assert out["open_interest_ccy"] == pytest.approx(2850.0)
    assert out["ls_ratio"] == pytest.approx(1.24)
    assert out["funding_avg_7d"] == pytest.approx(0.0003)  # the newest 21 settlements
    assert out["funding_pctile_30d"] == pytest.approx(69 / 90 * 100)
    assert out["oi_24h_pct"] == pytest.approx((1240 / 1000 - 1) * 100)
    assert out["oi_7d_pct"] == pytest.approx((2000 / 1300 - 1) * 100)
    assert out["taker_ratio"] == pytest.approx(1.5)
    assert out["hl_funding"] == pytest.approx(0.0000125 * 8)
    assert history[0] == pytest.approx(0.0001) and history[-1] == pytest.approx(0.0003)
    assert len(prov) == 7


def test_malformed_venue_rows_are_gaps_not_crashes(venues: dict[str, Any]) -> None:
    venues["taker"] = [["1", "n/a", "x"]]
    venues["ls"] = [["1", ""]]
    venues["hist"] = [{"fundingRate": ""}, {"fundingRate": None}]
    out, _, history = market.derivatives(SOL, _obs("hyperliquid", {"unexpected": True}))
    assert "taker_ratio" not in out
    assert out["ls_ratio"] is None
    assert "funding_avg_7d" not in out and history == []
    assert "hl_funding" not in out
    assert out["funding_8h"] == pytest.approx(0.0002)  # the rest still arrives


def test_one_unparseable_settlement_does_not_lose_the_derivatives_family(
    venues: dict[str, Any],
) -> None:
    """Same defect as D-18g on the per-asset path: `float(r["fundingRate"])` raised on one
    bad history row, `_unpack` then dropped the whole family - funding_8h included, which
    the gate needs."""
    venues["hist"] = [{"fundingRate": "abc"}, {"fundingRate": "0.0001"}]
    hl = _obs("hyperliquid", [{"universe": [{"name": "SOL"}]}, [{"funding": "n/a"}]])
    out, _, history = market.derivatives(SOL, hl)
    assert out["funding_8h"] == pytest.approx(0.0002)
    assert history == [pytest.approx(0.0001)]
    assert "hl_funding" not in out


def test_unlock_for_hands_the_emission_windows_and_the_largest_cliff_to_the_dossier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`unlock_for` passed only the next step, so pct_next_7d / pct_next_30d and the largest
    cliff in 30 days were computed and then dropped before any agent saw them."""
    day = 86_400
    start = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
    stamps = [start + i * day for i in range(40)]
    payload = {
        "documentedData": {
            "data": [
                {"label": "Base", "data": [{"timestamp": t, "unlocked": 100e6} for t in stamps]},
                {
                    "label": "Investors",
                    "data": [
                        {"timestamp": t, "unlocked": 2e6 if t >= start + 9 * day else 0.0}
                        for t in stamps
                    ],
                },
                {
                    "label": "Team",
                    "data": [
                        {"timestamp": t, "unlocked": 4e6 if t >= start + 30 * day else 0.0}
                        for t in stamps
                    ],
                },
            ]
        }
    }
    monkeypatch.setattr(unlocks, "fetch_schedule", lambda slug: _obs(f"unlocks.{slug}", payload))
    asset = Asset(
        symbol="SOL", okx="SOL-USDT-SWAP", coingecko="solana", source="core", unlocks_slug="solana"
    )
    out, prov = market.unlock_for(asset, NOW)
    assert out["unlock_in_days"] == 3 and out["unlock_pct_of_unlocked"] == pytest.approx(2.0)
    assert out["unlock_max_30d_in_days"] == 24
    assert out["unlock_max_30d_pct"] == pytest.approx(4.0)
    assert out["unlock_pct_next_7d"] == pytest.approx(2.0)
    assert out["unlock_pct_next_30d"] == pytest.approx(6.0)
    assert len(prov) == 1
