"""The manager's side of the Risk Gate: what it feeds `evaluate`, and what it does when that fails.

Review 2026-10-07 (G-N4, G-19): a missing price became `mark=0`, `evaluate()` raised outside any
handler, the run row stayed RUNNING and no later asset was gated. A gate that cannot reach a verdict
has reached one — REJECTED — and the run must carry on to say so.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.gate.fees import FeeRates
from argus.journal import ledger
from argus.journal.db import open_journal
from argus.runs.manager import run_manager
from tests.runs.test_manager_shadow import NOW, _deps, _market


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _gate_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT p.symbol, g.verdict, g.reasons FROM plans p"
                    " JOIN gate_results g ON g.plan_id = p.plan_id ORDER BY p.symbol"
                )
            ).mappings()
        ]


def _run_status(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT status FROM runs")).scalar())


def test_a_gate_that_raises_rejects_that_plan_and_the_run_carries_on(engine: Engine) -> None:
    def market(asset: Any, dossier: Any) -> Any:
        built = _market(asset.symbol, dossier.regime)
        # A positive maker rate (a VIP rebate, or a sign-convention change) makes `fees.py` raise
        # deep inside gate 7 — the unhandled path G-19 describes, reached with valid prices.
        rebate = FeeRates(maker=Decimal("0.0001"), taker=Decimal("-0.0005"))
        return dataclasses.replace(built, fees=rebate) if asset.symbol == "BTC" else built

    result = run_manager(engine, _deps(market=market), now=NOW, shadow=True)

    assert _run_status(engine) == "OK"
    rows = {r["symbol"]: r for r in _gate_rows(engine)}
    assert rows["BTC"]["verdict"] == "REJECTED"
    assert "gate_error" in rows["BTC"]["reasons"]
    # The asset after the failure was still judged — one bad input is not a dead run.
    assert rows["SOL"]["verdict"] in {"APPROVED", "ADJUSTED"}
    assert result.rejected == 1
    assert ledger.verify(engine, "gate_results")[0]


def test_a_plan_for_another_symbol_than_its_dossier_is_never_placed(engine: Engine) -> None:
    """L-9: the per-asset Trader path never checked `plan.symbol == dossier.symbol`. A BTC plan
    returned for the SOL dossier was gated on SOL's price and handed SOL's instrument id."""
    placed: list[tuple[str, str]] = []

    def execute(run_id: str, plan: Any, gate: Any, plan_id: str, instrument: str) -> str:
        placed.append((plan.symbol, instrument))
        return "order"

    from tests.runs.test_manager_shadow import _good_plan

    def proposer(run_id: str, d: Any, m: Any) -> Any:
        return _good_plan("BTC") if d.symbol == "SOL" else None

    deps = _deps(proposer=proposer, execute=execute)
    run_manager(engine, deps, now=NOW, shadow=False)

    assert all(instrument == f"{symbol}/USDT:USDT" for symbol, instrument in placed)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT d.symbol, g.verdict, g.reasons FROM plans p"
                " JOIN gate_results g ON g.plan_id = p.plan_id"
                " JOIN dossiers d ON d.dossier_id = json_extract(p.payload, '$.dossier_id')"
            )
        ).all()
    mismatched = [(v, r) for dossier_symbol, v, r in rows if dossier_symbol == "SOL"]
    assert mismatched and all(v == "REJECTED" and "researched as SOL" in r for v, r in mismatched)


def test_missing_market_inputs_reject_at_gate_two_instead_of_defaulting(engine: Engine) -> None:
    def market(asset: Any, dossier: Any) -> Any:
        built = _market(asset.symbol, dossier.regime)
        return dataclasses.replace(built, mark=None, spread_pct=None, funding_8h=None)

    run_manager(engine, _deps(market=market), now=NOW, shadow=True)

    for row in _gate_rows(engine):
        assert row["verdict"] == "REJECTED"
        reasons = json.loads(row["reasons"])["reasons"]
        rejected = [r for r in reasons if r["verdict"] == "REJECT"]
        assert rejected[0]["gate"] == 2
        assert "mark" in rejected[0]["detail"]


def test_the_manager_hands_the_order_path_a_symbol_the_okx_adapter_can_resolve(
    engine: Engine,
) -> None:
    """F-162: the manager passed `asset.okx` ("BTC-USDT-SWAP") to placement, but the OKX adapter
    looks instruments up among ccxt's markets, which are keyed "BTC/USDT:USDT". Every armed
    placement raised "not an OKX market", so no AI-approved plan could ever reach the venue."""
    from argus.exchange.okx import OKXAdapter
    from tests.runs.test_manager_shadow import _good_plan

    placed: list[str] = []

    def execute(run_id: str, plan: Any, gate: Any, plan_id: str, instrument: str) -> str:
        placed.append(instrument)
        return "order"

    deps = _deps(proposer=lambda run_id, d, m: _good_plan(d.symbol), execute=execute)
    run_manager(engine, deps, now=NOW, shadow=False)
    assert placed, "the fixture must approve at least one plan"

    adapter = OKXAdapter.__new__(OKXAdapter)
    adapter._markets_loaded = True
    adapter.client = type("C", (), {"markets": {i: {"id": i} for i in placed}})()
    for instrument in placed:
        assert adapter._market(instrument)["id"] == instrument
        assert "/" in instrument and instrument.endswith(":USDT")
