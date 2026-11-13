"""One full armed run: manager -> gate -> executor -> venue, against the fake exchange.

`tests/integration/` existed from the first commit and stayed **empty**, and every bug that
reached production this week lived in a seam rather than in a unit:

  F-127  the manager passed `asset.inst_id`; the field is `asset.okx`
  F-137  a long was translated to `buy`, then re-translated to `sell` at the adapter
  F-138  `place_approved` looked for `entry`; both adapters return `entry_id`

Each side of each seam was individually correct and tested. Nothing exercised the join, so nothing
failed until a real venue disagreed. This test is the join.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.exchange.fake import FakeExchange
from argus.execution.place import place_approved
from argus.gate.rules import GateResult
from argus.journal.db import open_journal
from argus.runs.manager import ManagerDeps, run_manager
from tests.runs.test_manager_shadow import (  # reuse the manager's own fixtures
    NOW,
    _assets,
    _dossier,
    _good_plan,
    _market,
    _regime,
)

BTC = "BTC/USDT:USDT"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _deps(venue: FakeExchange, engine: Engine, seen: list[dict[str, Any]]) -> ManagerDeps:
    def execute(
        run_id: str, plan: Any, gate: GateResult, plan_id: str, instrument: str
    ) -> str | None:
        seen.append({"symbol": plan.symbol, "side": plan.side, "instrument": instrument})
        return place_approved(engine, venue, run_id, plan, gate, plan_id, instrument)

    return ManagerDeps(
        assets=_assets,
        regime=lambda assets: (_regime(), []),
        dossier=lambda asset, regime: _dossier(asset, regime),
        market=lambda asset, dossier: _market(asset.symbol, dossier.regime),
        portfolio=lambda: (2000.0, 1800.0, []),
        propose=lambda run_id, dossier, market: _good_plan(dossier.symbol),
        execute=execute,
    )


def _armed(engine: Engine, venue: FakeExchange, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Force the gate to approve so the seam downstream of it is actually exercised."""
    from argus.gate.sizing import SizingResult
    from argus.runs import manager as module

    sized = SizingResult(
        ok=True,
        reason="",
        step=0,
        risk_usd=20.0,
        stop_dist_pct=4.0,
        notional_usd=600.0,
        leverage=2,
        margin_usd=300.0,
        contracts=Decimal("1"),
        liq_dist_pct=25.0,
    )
    monkeypatch.setattr(
        module, "evaluate", lambda *a, **k: GateResult(status="APPROVED", sized=sized)
    )
    seen: list[dict[str, Any]] = []
    result = run_manager(engine, _deps(venue, engine, seen), now=NOW, shadow=False)
    return result, seen


def test_an_approved_plan_becomes_a_real_order_at_the_venue(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue = FakeExchange()
    result, seen = _armed(engine, venue, monkeypatch)

    assert result.orders_placed > 0, "nothing reached the venue"
    assert len(venue.orders) == result.orders_placed
    order = next(iter(venue.orders.values()))
    # F-137: a long must arrive as a buy. Both translations happen between the plan and here.
    assert order.side == "buy"
    assert order.size > 0


def test_the_executor_receives_the_venue_instrument_id(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-127: the manager passed a field `Asset` does not have, and two approved plans were lost."""
    venue = FakeExchange()
    _result, seen = _armed(engine, venue, monkeypatch)

    assert seen, "the executor was never called"
    for call in seen:
        assert call["instrument"] == f"{call['symbol']}/USDT:USDT"


def test_the_order_is_journalled_with_the_venue_id(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-138: `place_approved` checked the wrong key, so a live order was recorded REJECTED."""
    venue = FakeExchange()
    _armed(engine, venue, monkeypatch)

    with engine.connect() as conn:
        rows = (
            conn.execute(text("SELECT exchange_id, cl_ord_id, side FROM orders")).mappings().all()
        )
    assert rows, "no order row was written"
    for row in rows:
        assert row["exchange_id"], "the venue id never reached the row"
        assert row["side"] == "buy"


def test_every_placed_order_carries_a_stop(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 4, end to end. The stop travels with the entry, so the venue holds one per order."""
    venue = FakeExchange()
    result, _ = _armed(engine, venue, monkeypatch)

    stops = [a for a in venue.algos.values() if a.kind == "sl" and a.state == "live"]
    assert len(stops) == result.orders_placed

    for order in venue.orders.values():
        stop = next(a for a in stops if a.symbol == order.symbol)
        # On the protective side of a long, which is the check OKX makes with sCode 51049.
        assert order.price is not None and stop.trigger_px < order.price


def test_a_shadow_run_places_nothing_even_when_the_gate_approves(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strongest claim in the system, exercised through the real seam rather than a stub."""
    from argus.gate.sizing import SizingResult
    from argus.runs import manager as module

    venue = FakeExchange()
    sized = SizingResult(
        ok=True,
        reason="",
        step=0,
        risk_usd=20.0,
        stop_dist_pct=4.0,
        notional_usd=600.0,
        leverage=2,
        margin_usd=300.0,
        contracts=Decimal("1"),
        liq_dist_pct=25.0,
    )
    monkeypatch.setattr(
        module, "evaluate", lambda *a, **k: GateResult(status="APPROVED", sized=sized)
    )
    seen: list[dict[str, Any]] = []
    result = run_manager(engine, _deps(venue, engine, seen), now=NOW, shadow=True)

    assert result.approved > 0, "the gate must approve, or this proves nothing"
    assert result.orders_placed == 0
    assert venue.orders == {}
    assert seen == []
