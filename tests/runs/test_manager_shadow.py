"""`argus manager --shadow`: the full pipeline, no orders (BUILD_PLAN Phase 3).

The run is injectable so the whole thing is exercised offline: regime → universe → dossiers →
proposer → Risk Gate → journal. Shadow runs place nothing and are marked as such in `runs`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.data.envelope import Observation
from argus.features.dossier import Dossier, assemble
from argus.features.regime import RegimeInputs, RegimeView, classify
from argus.features.universe import Asset
from argus.gate.fees import FeeRates
from argus.gate.rules import FactorScores, MarketContext, PlanInput, Target
from argus.gate.sizing import InstrumentSpec
from argus.journal import ledger
from argus.journal.db import open_journal
from argus.runs.manager import ManagerDeps, run_manager
from argus.runs.watchdog import write_halt

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)
SPEC = InstrumentSpec(
    contract_value=Decimal("1"),
    lot_size=Decimal("0.1"),
    min_contracts=Decimal("0.1"),
    max_leverage=50,
    maint_margin_rate=Decimal("0.01"),
)


def _regime() -> RegimeView:
    return classify(
        RegimeInputs(
            btc_close=100,
            btc_ema20=95,
            btc_ema50=90,
            btc_ema20_slope=0.5,
            btc_rv20_pct=40,
            breadth_20=60,
            next_event_hours=72,
        )
    )


def _assets() -> list[Asset]:
    return [
        Asset("BTC", "BTC-USDT-SWAP", "bitcoin", "core"),
        Asset("SOL", "SOL-USDT-SWAP", "solana", "core"),
    ]


def _dossier(asset: Asset, regime: RegimeView) -> Dossier:
    return assemble(
        symbol=asset.symbol,
        source=asset.source,
        now=NOW,
        regime=regime,
        technical={"price": 100.0, "atr_4h_pct": 2.5, "high_20_4h": 108.0, "low_20_4h": 95.0},
        derivatives={"funding_8h": 0.0001},
        provenance=[Observation(source="okx.public.ticker", tier=1, asof=NOW)],
    )


def _market(symbol: str, regime: RegimeView) -> MarketContext:
    return MarketContext(
        mark=100.0,
        atr_pct=2.5,
        spread_pct=0.01,
        funding_8h=0.0001,
        swing_high_20=108.0,
        swing_low_20=95.0,
        regime=regime,
        spec=SPEC,
        fees=FeeRates(maker=Decimal("-0.0002"), taker=Decimal("-0.0005")),
        consensus_ok=True,
        derivatives_age_min=5.0,
        next_event_hours=72.0,
        beta_btc=1.0,
    )


def _good_plan(symbol: str) -> PlanInput:
    return PlanInput(
        symbol=symbol,
        side="long",
        entry_kind="limit",
        entry_low=99.0,
        entry_high=101.0,
        stop=94.0,
        targets=[Target(110.0, 50), Target(120.0, 50)],
        time_stop_hours=120,
        scores=FactorScores(fundamental=1, onchain=1, macro=0, catalyst=1, technical=2),
    )


def _weak_plan(symbol: str) -> PlanInput:
    return PlanInput(
        symbol=symbol,
        side="long",
        entry_kind="limit",
        entry_low=99.0,
        entry_high=101.0,
        stop=94.0,
        targets=[Target(110.0, 50), Target(120.0, 50)],
        time_stop_hours=120,
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=1),
    )


def _deps(proposer: Any = None, **over: Any) -> ManagerDeps:
    base: dict[str, Any] = {
        "assets": _assets,
        "regime": lambda assets: (_regime(), []),
        "dossier": lambda asset, regime: _dossier(asset, regime),
        "market": lambda asset, dossier: _market(asset.symbol, dossier.regime),
        "portfolio": lambda: (2000.0, 1800.0, []),
        "propose": proposer or (lambda run_id, dossier, market: _good_plan(dossier.symbol)),
    }
    base.update(over)
    return ManagerDeps(**base)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _rows(engine: Engine, table: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table}")).mappings()]  # noqa: S608


def test_a_shadow_run_journals_every_stage_and_places_nothing(engine: Engine) -> None:
    result = run_manager(engine, _deps(), now=NOW, shadow=True)

    assert result.shadow and result.orders_placed == 0
    runs = _rows(engine, "runs")
    assert len(runs) == 1 and runs[0]["kind"] == "manager" and runs[0]["shadow"] == 1
    assert runs[0]["status"] == "OK"

    assert len(_rows(engine, "regime_snapshots")) == 1
    assert {d["symbol"] for d in _rows(engine, "dossiers")} == {"BTC", "SOL"}
    assert len(_rows(engine, "plans")) == 2
    assert len(_rows(engine, "gate_results")) == 2


def test_approved_plans_are_recorded_with_their_sizing(engine: Engine) -> None:
    run_manager(engine, _deps(), now=NOW, shadow=True)
    plan = _rows(engine, "plans")[0]
    gate = _rows(engine, "gate_results")[0]
    assert plan["state"] == "GATED"
    assert gate["verdict"] in {"APPROVED", "ADJUSTED"}
    assert '"notional_usd"' in gate["reasons"] or "notional" in gate["reasons"]


def test_rejected_plans_are_journalled_too_they_are_the_calibration_dataset(engine: Engine) -> None:
    run_manager(engine, _deps(proposer=lambda r, d, m: _weak_plan(d.symbol)), now=NOW, shadow=True)
    plans = _rows(engine, "plans")
    gates = _rows(engine, "gate_results")
    assert len(plans) == 2 and all(p["state"] == "REJECTED" for p in plans)
    assert all(g["verdict"] == "REJECTED" for g in gates)
    assert "score" in gates[0]["reasons"]


def test_the_traders_family_is_journaled_with_the_plan(engine: Engine) -> None:
    """The Reviewer must avoid the family that wrote the plan, and only the plan row survives."""
    import dataclasses
    import json

    def proposer(run_id: str, dossier: Any, market: Any) -> PlanInput:
        return dataclasses.replace(_good_plan(dossier.symbol), trader_family="google")

    run_manager(engine, _deps(proposer=proposer), now=NOW, shadow=True)
    payloads = [json.loads(p["payload"]) for p in _rows(engine, "plans")]
    assert payloads and {p["trader_family"] for p in payloads} == {"google"}


def test_no_trade_is_a_first_class_outcome(engine: Engine) -> None:
    # F-001: the prototype's "always produce a signal" prompt caused overtrading.
    result = run_manager(engine, _deps(proposer=lambda r, d, m: None), now=NOW, shadow=True)
    assert result.plans == 0 and result.approved == 0
    assert _rows(engine, "plans") == []
    assert len(_rows(engine, "dossiers")) == 2  # research still happened and is journalled


def test_the_ledger_chain_stays_valid_across_a_run(engine: Engine) -> None:
    run_manager(engine, _deps(), now=NOW, shadow=True)
    assert ledger.verify(engine, "plans") == (True, None)
    assert ledger.verify(engine, "gate_results") == (True, None)


def test_a_halt_file_stops_the_run_before_any_research(engine: Engine, tmp_path: Path) -> None:
    halt = tmp_path / "HALT"
    write_halt(halt, "manual /halt")
    result = run_manager(engine, _deps(), now=NOW, shadow=True, halt_file=halt)
    assert result.halted and result.plans == 0
    assert _rows(engine, "dossiers") == []
    assert _rows(engine, "runs")[0]["status"] == "HALTED"


def test_a_halt_written_mid_run_stops_every_placement_after_it(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager read HALT once, at its start. `argus halt` during the research phase — minutes
    of model calls — still let every approved plan be placed."""
    from argus.gate.rules import GateResult
    from argus.runs import manager as manager_module

    monkeypatch.setattr(
        manager_module, "evaluate", lambda *a, **k: GateResult(status="APPROVED", sized=None)
    )
    halt = tmp_path / "HALT"

    def propose(run_id: str, dossier: Any, market: Any) -> PlanInput:
        write_halt(halt, "operator: halted while the run was researching")
        return _good_plan(dossier.symbol)

    placed: list[str] = []
    deps = _deps(propose)
    deps.execute = lambda run_id, plan, gate, plan_id, instrument: (  # type: ignore[assignment]
        placed.append(plan.symbol) or "order-1"
    )
    result = run_manager(engine, deps, now=NOW, shadow=False, halt_file=halt)

    assert placed == []
    assert result.halted
    assert result.orders_placed == 0


def test_a_live_run_without_an_executor_is_refused(engine: Engine) -> None:
    """The second lock on live trading, and a structural one.

    `argus.config` already refuses MODE=auto|approve without LIVE_TRADING=I_UNDERSTAND. This
    catches the other shape of the mistake: a live run that was never handed a way to place
    orders would otherwise research every asset, approve a trade, and quietly place nothing —
    looking exactly like a run that decided not to trade.
    """
    with pytest.raises(NotImplementedError, match="executor"):
        run_manager(engine, _deps(), now=NOW, shadow=False)


def test_a_shadow_run_never_calls_the_executor(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handing shadow an executor must still place nothing — shadow is the stronger claim.

    The gate is forced to APPROVE here on purpose. An earlier version of this test passed because
    the stub's plans were all rejected on score, so the placement branch was never reached and the
    test proved nothing: shadow was relying on the executor happening to be absent.
    """
    calls: list[str] = []

    from argus.gate.rules import GateResult
    from argus.runs import manager as manager_module

    monkeypatch.setattr(
        manager_module,
        "evaluate",
        lambda *a, **k: GateResult(status="APPROVED", sized=None),
    )
    deps = _deps()
    deps.execute = lambda *a, **k: calls.append("placed") or "order-1"  # type: ignore[assignment]
    result = run_manager(engine, deps, now=NOW, shadow=True)
    assert result.approved > 0, "the gate must actually approve, or this test proves nothing"
    assert calls == []
    assert result.orders_placed == 0


def test_a_dossier_without_trade_critical_families_is_not_proposed_on(engine: Engine) -> None:
    def bare(asset: Asset, regime: RegimeView) -> Dossier:
        return assemble(symbol=asset.symbol, source="core", now=NOW, regime=regime)

    result = run_manager(engine, _deps(dossier=bare), now=NOW, shadow=True)
    assert result.plans == 0
    assert len(_rows(engine, "dossiers")) == 2
    assert result.skipped_for_gaps == 2


def test_one_bad_asset_does_not_abort_the_run(engine: Engine) -> None:
    def flaky(asset: Asset, regime: RegimeView) -> Dossier:
        if asset.symbol == "BTC":
            raise TimeoutError("exchange hiccup")
        return _dossier(asset, regime)

    result = run_manager(engine, _deps(dossier=flaky), now=NOW, shadow=True)
    assert result.errors == ["BTC: TimeoutError: exchange hiccup"]
    assert {d["symbol"] for d in _rows(engine, "dossiers")} == {"SOL"}
    assert _rows(engine, "runs")[0]["status"] == "OK"  # partial, not failed


def test_a_crash_after_setup_closes_the_row_failed_instead_of_leaving_it_running(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Research, journalling and the gate run outside the setup `try`. A raise there — 'database
    is locked', a TypeError from a stored value — left the row RUNNING with no alert (J-1)."""
    import argus.runs.manager as manager_module

    def locked(*_: Any, **__: Any) -> Any:
        raise RuntimeError("database is locked")

    # Not `evaluate`: a raise inside the gate is a per-asset REJECT by design (G-N4). A journal
    # write that fails is not a decision about one asset, so it must still fail the run.
    monkeypatch.setattr(manager_module, "_record_dossier", locked)
    result = run_manager(engine, _deps(), now=NOW, shadow=True)
    assert result.errors == ["RuntimeError: database is locked"]
    run = _rows(engine, "runs")[0]
    assert run["status"] == "FAILED"
    assert run["error"] == "RuntimeError: database is locked"


def test_the_regime_snapshot_keeps_its_inputs_and_gaps(engine: Engine) -> None:
    run_manager(engine, _deps(), now=NOW, shadow=True)
    snapshot = _rows(engine, "regime_snapshots")[0]
    assert snapshot["label"] == "TREND_UP"
    assert '"btc_close"' in snapshot["inputs"]
    assert "etf_5d_usd" in snapshot["gaps"]


def test_assets_are_researched_in_parallel_but_journalled_one_at_a_time(engine: Engine) -> None:
    # F-059: sequential research put a 15-asset run at the whole 8-minute budget.
    import threading

    concurrent = threading.Barrier(2, timeout=3)
    writers: list[str] = []
    lock = threading.Lock()

    def slow_dossier(asset: Asset, regime: RegimeView) -> Dossier:
        concurrent.wait()  # only returns if two assets are being researched at once
        with lock:
            writers.append(threading.current_thread().name)
        return _dossier(asset, regime)

    result = run_manager(engine, _deps(dossier=slow_dossier), now=NOW, shadow=True, workers=2)
    assert result.dossiers == 2
    assert len(set(writers)) == 2  # research really overlapped
    assert len(_rows(engine, "dossiers")) == 2


def test_parallel_research_keeps_error_isolation(engine: Engine) -> None:
    def flaky(asset: Asset, regime: RegimeView) -> Dossier:
        if asset.symbol == "BTC":
            raise TimeoutError("exchange hiccup")
        return _dossier(asset, regime)

    result = run_manager(engine, _deps(dossier=flaky), now=NOW, shadow=True, workers=2)
    assert result.errors == ["BTC: TimeoutError: exchange hiccup"]
    assert {d["symbol"] for d in _rows(engine, "dossiers")} == {"SOL"}


def test_results_are_journalled_in_a_stable_order_whatever_the_finish_order(engine: Engine) -> None:
    import time

    def uneven(asset: Asset, regime: RegimeView) -> Dossier:
        if asset.symbol == "BTC":
            time.sleep(0.05)  # finishes last
        return _dossier(asset, regime)

    run_manager(engine, _deps(dossier=uneven), now=NOW, shadow=True, workers=2)
    with engine.connect() as conn:
        order = [
            r[0]
            for r in conn.execute(text("SELECT symbol FROM dossiers ORDER BY rowid")).fetchall()
        ]
    assert order == ["BTC", "SOL"]  # universe order, so runs are comparable


def test_the_run_row_records_what_the_run_was_allowed_to_do(engine: Engine) -> None:
    """`shadow` was hardcoded to 1, so an ARMED run was journalled as having placed nothing.

    The audit trail has to say what a run was permitted to do, not what we hoped it would do —
    otherwise a run that placed real orders reads, forever, as one that could not have (F-125).
    """
    run_manager(engine, _deps(), now=NOW, shadow=True)
    assert _rows(engine, "runs")[-1]["shadow"] == 1

    deps = _deps()
    deps.execute = lambda *a, **k: None  # type: ignore[assignment]
    run_manager(engine, deps, now=NOW, shadow=False)
    row = _rows(engine, "runs")[-1]
    assert row["shadow"] == 0
    # The venue travels with it: `mode` alone cannot tell demo money from real money.
    assert row["config_hash"] in ("demo", "live")


def test_the_executor_is_handed_the_venue_instrument_id(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first armed run approved two plans and placed neither, because the manager passed
    `asset.inst_id` — a field `Asset` does not have (F-127).

    The placement tests all passed an instrument explicitly, so nothing exercised the handoff
    between the manager and the executor. This closes that seam: the executor must receive the
    instrument the OKX adapter resolves — ccxt's "BTC/USDT:USDT". F-127 pinned OKX's REST id
    ("BTC-USDT-SWAP") here, which the adapter cannot find, so placement still failed (F-162).
    """
    seen: list[tuple[str, str]] = []

    from argus.gate.rules import GateResult
    from argus.runs import manager as manager_module

    monkeypatch.setattr(
        manager_module, "evaluate", lambda *a, **k: GateResult(status="APPROVED", sized=None)
    )
    deps = _deps()
    deps.execute = lambda run_id, plan, gate, plan_id, instrument: (  # type: ignore[assignment]
        seen.append((plan.symbol, instrument)) or "order-1"
    )
    result = run_manager(engine, deps, now=NOW, shadow=False)

    assert result.orders_placed == len(seen) > 0
    for symbol, instrument in seen:
        assert instrument == f"{symbol}/USDT:USDT", "the executor needs ccxt's symbol (F-162)"


def test_the_batch_proposer_is_asked_once_for_the_whole_run(engine: Engine) -> None:
    """The point of F-117: one call that sees every candidate, not one call per asset.

    Fifteen independent calls cannot rank anything — each answers "is this tradeable?" alone, so
    the gate approves the first plan that passes rather than the best one available.
    """
    calls: list[int] = []

    def batch(run_id: str, candidates: list[Any]) -> dict[str, Any]:
        calls.append(len(candidates))
        return {candidates[0][0].symbol: _good_plan(candidates[0][0].symbol)}

    deps = _deps()
    deps.propose_batch = batch  # type: ignore[assignment]
    result = run_manager(engine, deps, now=NOW, shadow=True)

    assert calls == [2], "asked once, with every researched asset"
    # Only the symbol the Trader picked becomes a plan; the rest are a refusal by omission.
    assert result.plans == 1


def test_the_per_asset_proposer_is_not_called_when_batching(engine: Engine) -> None:
    asked: list[str] = []

    def single(run_id: str, dossier: Any, market: Any) -> Any:
        asked.append(dossier.symbol)
        return _good_plan(dossier.symbol)

    deps = _deps(proposer=single)
    deps.propose_batch = lambda run_id, candidates: {}  # type: ignore[assignment]
    run_manager(engine, deps, now=NOW, shadow=True)
    assert asked == [], "batching must not also pay for the per-asset calls"


def test_a_failing_batch_proposer_does_not_kill_the_run(engine: Engine) -> None:
    """No plans is a result. A dead run is not."""

    def boom(run_id: str, candidates: list[Any]) -> dict[str, Any]:
        raise RuntimeError("every model failed")

    deps = _deps()
    deps.propose_batch = boom  # type: ignore[assignment]
    result = run_manager(engine, deps, now=NOW, shadow=True)
    assert result.plans == 0
    assert any("propose" in e for e in result.errors)
    assert _rows(engine, "runs")[-1]["status"] == "OK"


def test_an_asset_the_venue_does_not_list_is_skipped_not_a_failed_run(engine: Engine) -> None:
    """Found on the first deploy of 2026-10-07: OKX's demo venue lists no ZEC, HYPE or UNI swap.

    Armed runs now read fee rates per asset (G-14), so those three raised 'not an OKX market', and
    the strict exit (J-N4) failed every run and alerted, although nothing was wrong with the 12
    assets the venue does list. An instrument the venue cannot trade is a fact about the venue:
    noted and skipped, never an error — while a real error on another asset still is one.
    """
    from argus.exchange.errors import InstrumentNotListedError

    def market(asset: Asset, dossier: Dossier) -> Any:
        if asset.symbol == "SOL":
            raise InstrumentNotListedError("SOL/USDT:USDT is not an OKX market")
        return _market(asset.symbol, dossier.regime)

    result = run_manager(engine, _deps(market=market), now=NOW, shadow=True)

    assert result.errors == []
    assert result.not_listed == ["SOL"]
    assert _rows(engine, "runs")[0]["status"] == "OK"
    assert "not listed" in result.summary()
