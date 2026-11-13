"""The manager's market builder: what the gate is told about price, spread and data quality.

Review 2026-10-07 D-1 / D-3: `mark` was the last *closed 4H close* (up to four hours old) and the
spread a constant 0.02 % because nothing ever wrote `spread_pct`. Sizing, the entry-distance check
and the urgent-entry spread cap all ran on numbers that were not the market.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

import argus.features.market as mkt
from argus.data.envelope import Observation
from argus.features.universe import Asset
from argus.gate.fees import FeeRates
from argus.gate.sizing import InstrumentSpec
from tests.runs.test_manager_shadow import NOW, _dossier, _regime

ASSET = Asset("SOL", "SOL-USDT-SWAP", "solana", "core")
SPEC = InstrumentSpec(
    contract_value=Decimal("1"),
    lot_size=Decimal("0.1"),
    min_contracts=Decimal("0.1"),
    max_leverage=50,
    maint_margin_rate=Decimal("0.01"),
)
FEES = FeeRates(maker=Decimal("-0.0002"), taker=Decimal("-0.0005"))


@pytest.fixture(autouse=True)
def _offline_venues(monkeypatch: pytest.MonkeyPatch) -> None:
    """`live_quote` also reads Bybit, Binance and Hyperliquid for consensus (D-2); never the net.
    Consensus itself is covered in `test_price_consensus.py`."""
    from argus.data.fetchers import binance_futures_public, bybit_kz_public, hyperliquid

    def down(*args: Any, **kwargs: Any) -> Observation:
        return Observation.missing("venue", 1, "offline test")

    for module, name in (
        (bybit_kz_public, "fetch_ticker"),
        (binance_futures_public, "fetch_premium_index"),
        (hyperliquid, "fetch_meta_and_contexts"),
    ):
        monkeypatch.setattr(module, name, down)


def _ticker(**row: Any) -> Observation:
    return Observation(
        source="okx.public.ticker",
        tier=1,
        asof=datetime(2026, 9, 18, 11, 59, tzinfo=UTC),
        data={"code": "0", "data": [row]},
    )


def test_the_quote_comes_from_a_fresh_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fetch(*args: Any, **kwargs: Any) -> Observation:
        calls.append(kwargs)
        return _ticker(last="104.5", bidPx="104.4", askPx="104.6")

    monkeypatch.setattr(mkt, "fetch_json", fetch)
    quote = mkt.live_quote(ASSET)

    assert quote.mark == pytest.approx(104.5)
    assert quote.spread_pct == pytest.approx(0.2 / 104.5 * 100)
    # A cached ticker is the stale price this replaces: the fetch must bypass the cache.
    assert calls and calls[0].get("force") is True
    assert calls[0]["params"] == {"instId": "SOL-USDT-SWAP"}


def test_a_ticker_without_a_book_has_no_spread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mkt, "fetch_json", lambda *a, **k: _ticker(last="104.5", bidPx=""))
    quote = mkt.live_quote(ASSET)
    assert quote.mark == pytest.approx(104.5) and quote.spread_pct is None


def test_a_failed_ticker_leaves_the_price_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = Observation.missing("okx.public.ticker", 1, "timeout")
    monkeypatch.setattr(mkt, "fetch_json", lambda *a, **k: missing)
    quote = mkt.live_quote(ASSET)
    assert quote.mark is None and quote.spread_pct is None


def test_the_gate_sees_the_live_mark_not_the_last_4h_close() -> None:
    dossier = _dossier(ASSET, _regime())  # technical price 100.0: the closed 4H bar
    quote = mkt.Quote(mark=104.5, spread_pct=0.05, consensus_ok=True)

    market = mkt.market_context(ASSET, dossier, SPEC, FEES, quote)

    assert market.mark == pytest.approx(104.5)
    assert market.spread_pct == pytest.approx(0.05)


def test_an_unknown_quote_reaches_the_gate_as_unknown() -> None:
    dossier = _dossier(ASSET, _regime())
    market = mkt.market_context(
        ASSET, dossier, SPEC, FEES, mkt.Quote(mark=None, spread_pct=None, consensus_ok=False)
    )
    assert market.mark is None and market.spread_pct is None


def _instrument(state: str) -> Observation:
    row = {"instId": "SOL-USDT-SWAP", "ctVal": "1", "lotSz": "0.1", "minSz": "0.1", "lever": "50"}
    return Observation(
        source="okx.public.instrument", tier=1, data={"data": [{**row, "state": state}]}
    )


@pytest.mark.parametrize(("state", "listed"), [("live", True), ("suspend", False)])
def test_the_market_list_check_reads_the_instrument_state(
    monkeypatch: pytest.MonkeyPatch, state: str, listed: bool
) -> None:
    """G-14: `in_market_list` was never set, so gate 10's list check could not fire."""
    monkeypatch.setattr(mkt, "fetch_json", lambda *a, **k: _instrument(state))
    assert mkt.instrument_listed(ASSET) is listed
    market = mkt.market_context(
        ASSET,
        _dossier(ASSET, _regime()),
        SPEC,
        FEES,
        mkt.Quote(100.0, 0.01, consensus_ok=True),
        listed=listed,
    )
    assert market.in_market_list is listed


class _FeeAdapter:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.asked: list[str] = []

    def fetch_fee_rates(self, symbol: str) -> dict[str, Decimal]:
        self.asked.append(symbol)
        if self.fail:
            raise ConnectionError("account endpoint down")
        return {"maker": Decimal("-0.0001"), "taker": Decimal("-0.0004")}


def test_fee_rates_are_read_for_the_asset_being_gated() -> None:
    adapter = _FeeAdapter(fail=False)
    rates = mkt.fee_rates(ASSET, allow_default=False, adapter=adapter)
    assert adapter.asked == ["SOL/USDT:USDT"]
    assert rates == FeeRates(maker=Decimal("-0.0001"), taker=Decimal("-0.0004"))


def test_an_armed_run_does_not_trade_on_guessed_fee_rates() -> None:
    """G-14 / F-026: the hard-coded fallback was 6-12x below real rates. Shadow may guess;
    an armed run raises, and the asset is skipped with the error recorded."""
    with pytest.raises(ConnectionError):
        mkt.fee_rates(ASSET, allow_default=False, adapter=_FeeAdapter(fail=True))
    guessed = mkt.fee_rates(ASSET, allow_default=True, adapter=_FeeAdapter(fail=True))
    assert guessed.taker < 0 and guessed.maker < 0


def test_only_confirmed_dated_catalysts_can_back_an_event_thesis() -> None:
    from argus.features.dossier import assemble

    dossier = assemble(
        symbol="SOL",
        source="solana",
        now=NOW,
        regime=_regime(),
        technical={"price": 100.0},
        derivatives={"funding_8h": 0.0001},
        catalysts=[
            {"date": NOW.date(), "symbol": "SOL", "kind": "upgrade", "status": "confirmed"},
            {"date": NOW.date(), "symbol": "SOL", "kind": "listing", "status": "rumoured"},
            {"date": NOW.date(), "symbol": "ETH", "kind": "upgrade", "status": "confirmed"},
        ],
    )
    market = mkt.market_context(
        ASSET, dossier, SPEC, FEES, mkt.Quote(100.0, 0.01, consensus_ok=True)
    )
    assert market.catalyst_dates == (NOW.date(),)


def test_the_funding_interval_comes_from_the_venue_schedule() -> None:
    hour = 3_600_000
    assert mkt.funding_interval_hours({"fundingTime": "0", "nextFundingTime": str(4 * hour)}) == 4.0
    assert mkt.funding_interval_hours({"fundingTime": str(hour), "nextFundingTime": ""}) is None
    assert mkt.funding_interval_hours({}) is None

    dossier = _dossier(ASSET, _regime())
    dossier.derivatives["funding_interval_h"] = 2.0
    market = mkt.market_context(
        ASSET, dossier, SPEC, FEES, mkt.Quote(100.0, 0.01, consensus_ok=True)
    )
    assert market.funding_interval_hours == 2.0


def test_now_is_the_dossier_clock() -> None:
    """Guard: the fixtures above are built at the manager tests' NOW."""
    assert _dossier(ASSET, _regime()).as_of == NOW


def test_the_mark_is_read_again_when_the_plan_is_gated(tmp_path: Any) -> None:
    """G-N3 / D-1: the quote was read during research, before the Trader, Red Team and Risk
    Debate calls - minutes before `evaluate` in a 480 s run. Band [99, 101], mark 100.5 when
    researched, 103 by the time the plan is gated: a market entry there fills far past the band
    the trade was sized on, so it must be judged at 103."""
    import dataclasses

    from argus.gate.rules import PlanInput, Target
    from argus.journal.db import open_journal
    from argus.runs.manager import run_manager
    from tests.runs.test_manager_shadow import _deps, _good_plan, _market

    engine = open_journal(tmp_path / "journal.db")
    researched = dataclasses.replace(_market("BTC", _regime()), mark=100.5)
    urgent: PlanInput = dataclasses.replace(
        _good_plan("BTC"),
        entry_kind="market",
        urgent=True,
        targets=[Target(112.0, 50), Target(124.0, 50)],
    )
    asked: list[str] = []

    def quote(asset: Asset) -> mkt.Quote:
        asked.append(asset.symbol)
        return mkt.Quote(mark=103.0, spread_pct=0.01, consensus_ok=True)

    deps = _deps(
        assets=lambda: [Asset("BTC", "BTC-USDT-SWAP", "bitcoin", "core")],
        market=lambda asset, dossier: researched,
        proposer=lambda run_id, dossier, market: urgent,
        quote=quote,
    )
    run_manager(engine, deps, now=NOW, shadow=True)

    from sqlalchemy import text

    with engine.connect() as conn:
        verdict, reasons, payload = conn.execute(
            text(
                "SELECT g.verdict, g.reasons, p.payload FROM gate_results g"
                " JOIN plans p ON p.plan_id = g.plan_id"
            )
        ).one()
    import json

    assert asked == ["BTC"]
    assert verdict == "REJECTED" and "outside the planned band" in reasons
    assert json.loads(payload)["market"]["mark"] == 103.0  # the journal shows what was gated
