"""Cross-venue price consensus reaches the Risk Gate (review D-2, FAILURE_LOG F-010).

F-059 took `build_snapshot` out of the run path, which left `consensus_price` with no caller, and
`MarketContext.consensus_ok` defaulted to True. Gate 2's check could not fire: a wrong OKX price
was cross-checked against nothing and the gate journalled "consensus ok".

The rule (DATA_SOURCES.md, DECISION_LOGIC.md gate 2): a majority of at least three sources
agreeing within 1 %, venue prices first. Fewer than three readable sources is not consensus, so it
is a REJECT like any other unknown gate input.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

import argus.features.market as mkt
from argus.data.envelope import Observation
from argus.data.fetchers import binance_futures_public, bybit_kz_public, hyperliquid
from argus.features.universe import Asset
from argus.gate.rules import MarketContext
from tests.runs.test_manager_shadow import NOW, _deps, _dossier, _market, _regime

ASSET = Asset("BTC", "BTC-USDT-SWAP", "bitcoin", "core")


def _venues(
    monkeypatch: pytest.MonkeyPatch,
    *,
    okx: str | None = "100.0",
    bybit: str | None = "100.1",
    binance: str | None = "99.95",
    hl: str | None = "100.05",
) -> list[tuple[str, str, bool]]:
    """Stub every venue the quote reads. None means that venue could not be reached."""
    asked: list[tuple[str, str, bool]] = []

    def okx_ticker(*args: Any, **kwargs: Any) -> Observation:
        if okx is None:
            return Observation.missing("okx.public.ticker", 1, "timeout")
        last = float(okx)
        row = {"last": okx, "bidPx": str(last - 0.01), "askPx": str(last + 0.01)}
        return Observation(source="okx.public.ticker", tier=1, data={"code": "0", "data": [row]})

    def bybit_ticker(symbol: str, *, force: bool = False) -> Observation:
        asked.append(("bybit", symbol, force))
        if bybit is None:
            return Observation.missing("bybit.kz.public.ticker", 1, "timeout")
        payload = {"retCode": 0, "result": {"list": [{"symbol": symbol, "lastPrice": bybit}]}}
        return Observation(source="bybit.kz.public.ticker", tier=1, data=payload)

    def binance_premium(symbol: str, *, force: bool = False) -> Observation:
        asked.append(("binance", symbol, force))
        if binance is None:
            return Observation.missing("binance.futures.public.premium_index", 1, "451")
        payload = {"symbol": symbol, "markPrice": binance}
        return Observation(source="binance.futures.public.premium_index", tier=1, data=payload)

    def hl_contexts() -> Observation:
        if hl is None:
            return Observation.missing("hyperliquid.meta_and_asset_ctxs", 1, "timeout")
        payload = [
            {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
            [{"markPx": "1"}, {"markPx": hl}],
        ]
        return Observation(source="hyperliquid.meta_and_asset_ctxs", tier=1, data=payload)

    monkeypatch.setattr(mkt, "fetch_json", okx_ticker)
    monkeypatch.setattr(bybit_kz_public, "fetch_ticker", bybit_ticker)
    monkeypatch.setattr(binance_futures_public, "fetch_premium_index", binance_premium)
    monkeypatch.setattr(hyperliquid, "fetch_meta_and_contexts", hl_contexts)
    return asked


# --- the quote ---------------------------------------------------------------------------------


def test_agreeing_venues_give_consensus(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _venues(monkeypatch)
    quote = mkt.live_quote(ASSET)
    assert quote.mark == pytest.approx(100.0)
    assert quote.consensus_ok is True
    # The cross-check is read as fresh as the OKX mark it checks: a 15-minute cached venue price
    # is the stale-aggregator failure F-010 is about.
    assert ("bybit", "BTCUSDT", True) in asked
    assert ("binance", "BTCUSDT", True) in asked


def test_one_outlying_venue_is_outvoted(monkeypatch: pytest.MonkeyPatch) -> None:
    _venues(monkeypatch, hl="103.0")
    assert mkt.live_quote(ASSET).consensus_ok is True  # OKX, Bybit, Binance agree


def test_an_okx_price_the_other_venues_contradict_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three venues agree, but not with the price the gate would size on."""
    _venues(monkeypatch, okx="102.5")
    quote = mkt.live_quote(ASSET)
    assert quote.mark == pytest.approx(102.5)
    assert quote.consensus_ok is False


def test_venues_that_disagree_with_each_other_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _venues(monkeypatch, bybit="102.0", binance="98.0", hl="104.0")
    assert mkt.live_quote(ASSET).consensus_ok is False


@pytest.mark.parametrize(
    "down",
    [
        {"bybit": None, "binance": None},
        {"binance": None, "hl": None},
        {"bybit": None, "binance": None, "hl": None},
    ],
)
def test_fewer_than_three_sources_is_not_consensus(
    monkeypatch: pytest.MonkeyPatch, down: dict[str, None]
) -> None:
    _venues(monkeypatch, **down)
    assert mkt.live_quote(ASSET).consensus_ok is False


def test_one_venue_down_still_leaves_three(monkeypatch: pytest.MonkeyPatch) -> None:
    _venues(monkeypatch, hl=None)
    assert mkt.live_quote(ASSET).consensus_ok is True


def test_an_unreadable_okx_price_has_no_consensus(monkeypatch: pytest.MonkeyPatch) -> None:
    _venues(monkeypatch, okx=None)
    quote = mkt.live_quote(ASSET)
    assert quote.mark is None and quote.consensus_ok is False


# --- the builder and the gate's input ----------------------------------------------------------


@pytest.mark.parametrize("agreed", [True, False])
def test_the_market_context_carries_the_quote_consensus(agreed: bool) -> None:
    quote = mkt.Quote(mark=100.0, spread_pct=0.01, consensus_ok=agreed)
    market = mkt.market_context(ASSET, _dossier(ASSET, _regime()), *_spec_fees(), quote)
    assert market.consensus_ok is agreed


def test_consensus_has_no_default_anywhere() -> None:
    """A default of True is how the check went dead; a builder must state what it measured."""
    for cls in (MarketContext, mkt.Quote):
        field = {f.name: f for f in dataclasses.fields(cls)}["consensus_ok"]
        assert field.default is dataclasses.MISSING, cls.__name__


def _spec_fees() -> tuple[Any, Any]:
    research = _market("BTC", _regime())
    return research.spec, research.fees


# --- end to end: the manager gates on the consensus read at gate time --------------------------


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **venues: str | None) -> tuple[str, str]:
    from argus.journal.db import open_journal
    from argus.runs.manager import run_manager

    _venues(monkeypatch, **venues)
    engine = open_journal(tmp_path / "journal.db")
    researched = dataclasses.replace(_market("BTC", _regime()), consensus_ok=True)
    deps = _deps(
        assets=lambda: [ASSET],
        market=lambda asset, dossier: researched,
        quote=mkt.live_quote,
    )
    run_manager(engine, deps, now=NOW, shadow=True)
    with engine.connect() as conn:
        verdict, reasons, payload = conn.execute(
            text(
                "SELECT g.verdict, g.reasons, p.payload FROM gate_results g"
                " JOIN plans p ON p.plan_id = g.plan_id"
            )
        ).one()
    assert json.loads(payload)["market"]["consensus_ok"] is ("consensus failed" not in reasons)
    return str(verdict), str(reasons)


def test_a_divergent_venue_rejects_at_gate_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agreed when researched; by the time the plan is gated OKX has left the other venues."""
    verdict, reasons = _gate(tmp_path, monkeypatch, okx="100.0", bybit="98.0", binance="98.1")
    assert verdict == "REJECTED"
    assert "price consensus failed" in reasons


def test_missing_venues_reject_at_gate_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verdict, reasons = _gate(tmp_path, monkeypatch, bybit=None, binance=None)
    assert verdict == "REJECTED"
    assert "price consensus failed" in reasons


def test_agreeing_venues_pass_gate_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    verdict, reasons = _gate(tmp_path, monkeypatch)
    assert "price consensus" not in reasons
    assert verdict != "REJECTED", reasons


def test_missing_venues_are_named_in_the_rejection_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead venue must be visible: otherwise a blocked host looks like a market disagreement."""
    from structlog.testing import capture_logs

    _venues(monkeypatch, bybit=None, binance=None)
    with capture_logs() as logs:
        assert mkt.live_quote(ASSET).consensus_ok is False
    event = next(e for e in logs if e["event"] == "quote.no_price_consensus")
    assert sorted(event["missing"]) == ["binance.futures.public", "bybit.kz.public"]


# --- per-1000 contracts (F-167) ----------------------------------------------------------------

PEPE = Asset("PEPE", "PEPE-USDT-SWAP", "pepe", "satellite")


def test_pepe_reaches_consensus_through_the_per_1000_venue_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binance and Bybit list PEPE as 1000PEPEUSDT and Hyperliquid as kPEPE, each quoting the
    price of 1000 PEPE (measured 2026-11-11: 0.00494 vs OKX 0.0000049436). Asking for "PEPEUSDT"
    got Binance's "Invalid symbol" on every run since PEPE joined the whitelist (F-167), so only
    OKX ever agreed and gate 2 refused every PEPE plan."""
    okx_last = "0.0000049436"

    def okx_ticker(*args: Any, **kwargs: Any) -> Observation:
        row = {"last": okx_last, "bidPx": "0.0000049430", "askPx": "0.0000049440"}
        return Observation(source="okx.public.ticker", tier=1, data={"code": "0", "data": [row]})

    def bybit_ticker(symbol: str, *, force: bool = False) -> Observation:
        if symbol != "1000PEPEUSDT":
            return Observation.missing("bybit.kz.public.ticker", 1, "symbol invalid")
        payload = {"retCode": 0, "result": {"list": [{"symbol": symbol, "lastPrice": "0.0049420"}]}}
        return Observation(source="bybit.kz.public.ticker", tier=1, data=payload)

    def binance_premium(symbol: str, *, force: bool = False) -> Observation:
        if symbol != "1000PEPEUSDT":
            return Observation.missing("binance.futures.public.premium_index", 1, "-1121")
        payload = {"symbol": symbol, "markPrice": "0.00494480"}
        return Observation(source="binance.futures.public.premium_index", tier=1, data=payload)

    def hl_contexts() -> Observation:
        payload = [
            {"universe": [{"name": "BTC"}, {"name": "kPEPE"}]},
            [{"markPx": "1"}, {"markPx": "0.004943"}],
        ]
        return Observation(source="hyperliquid.meta_and_asset_ctxs", tier=1, data=payload)

    monkeypatch.setattr(mkt, "fetch_json", okx_ticker)
    monkeypatch.setattr(bybit_kz_public, "fetch_ticker", bybit_ticker)
    monkeypatch.setattr(binance_futures_public, "fetch_premium_index", binance_premium)
    monkeypatch.setattr(hyperliquid, "fetch_meta_and_contexts", hl_contexts)

    quote = mkt.live_quote(PEPE)

    assert quote.consensus_ok is True
    assert quote.mark is not None and abs(quote.mark - float(okx_last)) < 1e-12
