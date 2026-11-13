"""Placing an order. Every test here is about the rule that a position is never left naked."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from argus.execution.orders import UnprotectedOrderError
from argus.execution.place import PlacementError, place_approved
from argus.gate.rules import GateResult, PlanInput, Target
from argus.gate.sizing import SizingResult
from argus.journal.db import open_journal
from argus.llm.schemas import FactorScores


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A journal with a run and two plan rows already in it.

    `orders` has foreign keys to both, which is the point: an order belonging to no run is an
    order nobody can explain afterwards. Seeding here keeps every test honest about that.
    """
    eng = open_journal(tmp_path / "journal.db")
    _seed(eng, "run-1", "plan-1")
    _seed(eng, "run-1", "p")
    _seed(eng, "run-1", "p1")
    _seed(eng, "run-1", "p2")
    return eng


def _seed(engine: Engine, run_id: str, plan_id: str) -> None:
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                " VALUES (:r,'manager','demo',:t,'RUNNING','x')"
            ),
            {"r": run_id, "t": stamp},
        )
        conn.execute(
            text(
                "INSERT OR IGNORE INTO plans (plan_id, run_id, created_at, mode, symbol, side,"
                " state, payload, prev_hash, row_hash)"
                " VALUES (:p,:r,:t,'demo','SOL','long','GATED','{}','','')"
            ),
            {"p": plan_id, "r": run_id, "t": stamp},
        )


class FakeVenue:
    """Records what it was asked to do, and can be told to refuse."""

    def __init__(self, fail: Exception | None = None, ids: dict[str, str] | None = None) -> None:
        self.fail, self.calls = fail, []  # type: ignore[var-annotated]
        # The key BOTH real adapters return. Using a different one here is what let a
        # successful placement be journalled REJECTED for a live order (F-138).
        self.ids = {"entry_id": "venue-1"} if ids is None else ids

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.calls.append(("set_leverage", symbol, leverage))

    def place_bracket(self, order: dict[str, Any]) -> dict[str, str]:
        self.calls.append(("place_bracket", order))
        if self.fail is not None:
            raise self.fail
        return self.ids


def _plan(side: str = "long", stop: float = 96.0) -> PlanInput:
    return PlanInput(
        symbol="SOL",
        side=side,  # type: ignore[arg-type]
        entry_kind="limit",
        entry_low=99.8,
        entry_high=100.2,
        stop=stop,
        targets=[Target(price=110.0, close_pct=100)],
        time_stop_hours=96,
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
    )


def _sized(ok: bool = True, contracts: str = "3") -> SizingResult:
    return SizingResult(
        ok=ok,
        reason="" if ok else "below minimum notional",
        step=0,
        risk_usd=10.0,
        stop_dist_pct=4.0,
        notional_usd=300.0,
        leverage=2,
        margin_usd=150.0,
        contracts=Decimal(contracts),
        liq_dist_pct=20.0,
    )


def _gate(status: str = "APPROVED", sized: SizingResult | None = None) -> GateResult:
    return GateResult(status=status, sized=sized if sized is not None else _sized())  # type: ignore[arg-type]


def _orders(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(text("SELECT * FROM orders ORDER BY created_at")).mappings().all()
        ]


def _latest_state(engine: Engine) -> str:
    """An order's state is DERIVED, not stored.

    `orders` holds one write-once row per order and every transition appends to
    `position_events`, so the history survives even when the latest state turns out to be wrong.
    Reading `orders.state` would always say NEW — the same shape as `order_state` being derived
    rather than updated in place.
    """
    from argus.execution.lifecycle import order_state

    rows = _orders(engine)
    return order_state(engine, rows[-1]["order_id"]) if rows else ""


def test_the_stop_travels_with_the_entry(engine: Engine) -> None:
    """Not a follow-up call. The venue refuses an entry it cannot protect, so there is no window
    between "filled" and "protected" for a crash to land in (CLAUDE.md #4)."""
    venue = FakeVenue()
    place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", "SOL-USDT-SWAP")

    order = next(c[1] for c in venue.calls if c[0] == "place_bracket")
    kinds = [a["kind"] for a in order["attach_algos"]]
    assert "sl" in kinds
    assert order["attach_algos"][0]["trigger_px"] == Decimal("96.0")


def test_the_order_is_journalled_before_it_is_sent(engine: Engine) -> None:
    """A row with no order is an orphan reconcile can find. An order with no row is money the
    system does not know it spent."""
    venue = FakeVenue(fail=RuntimeError("venue exploded"))
    with pytest.raises(PlacementError):
        place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", "SOL-USDT-SWAP")

    # The row exists — written before the venue was called — and its derived state records the
    # refusal. That first write is the evidence that we tried, before we knew the outcome.
    assert len(_orders(engine)) == 1
    assert _latest_state(engine) == "REJECTED"


def test_a_venue_refusal_is_recorded_with_its_reason(engine: Engine) -> None:
    venue = FakeVenue(fail=RuntimeError("insufficient margin"))
    with pytest.raises(PlacementError, match="insufficient margin"):
        place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", "SOL-USDT-SWAP")


def test_an_order_with_no_venue_id_is_a_failure(engine: Engine) -> None:
    """Without an id we could never amend, cancel or reconcile it — an untrackable position is
    worse than no position."""
    venue = FakeVenue(ids={})
    with pytest.raises(PlacementError, match="no order id"):
        place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", "SOL-USDT-SWAP")
    assert _latest_state(engine) == "REJECTED"


def test_a_rejected_plan_is_never_placed(engine: Engine) -> None:
    venue = FakeVenue()
    assert (
        place_approved(engine, venue, "run-1", _plan(), _gate("REJECTED"), "p", "SOL-USDT-SWAP")
        is None
    )
    assert venue.calls == []
    assert _orders(engine) == []


def test_an_adjusted_plan_is_placed(engine: Engine) -> None:
    """ADJUSTED is an approval on the gate's own terms — refusing it would discard a decision
    the gate already made."""
    venue = FakeVenue()
    assert place_approved(engine, venue, "run-1", _plan(), _gate("ADJUSTED"), "p", "SOL-USDT-SWAP")
    assert any(c[0] == "place_bracket" for c in venue.calls)


def test_an_approval_the_gate_could_not_size_is_a_bug_not_a_trade(engine: Engine) -> None:
    venue = FakeVenue()
    with pytest.raises(PlacementError, match="no usable size"):
        place_approved(engine, venue, "run-1", _plan(), _gate(sized=_sized(ok=False)), "p", "X")
    assert venue.calls == []


def test_a_stop_on_the_wrong_side_never_reaches_the_venue(engine: Engine) -> None:
    """Refused while building the payload, before anything is journalled or sent."""
    venue = FakeVenue()
    with pytest.raises(UnprotectedOrderError):
        place_approved(
            engine, venue, "run-1", _plan(stop=101.0), _gate(), "plan-1", "SOL-USDT-SWAP"
        )
    assert venue.calls == []
    assert _orders(engine) == []


def test_leverage_is_the_gate_s_number(engine: Engine) -> None:
    venue = FakeVenue()
    place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", "SOL-USDT-SWAP")
    assert ("set_leverage", "SOL-USDT-SWAP", 2) in venue.calls


def test_two_runs_of_the_same_plan_get_different_client_ids(engine: Engine) -> None:
    """`cl_ord_id` is derived from the run: a new run is a new intention with a new id.

    Idempotency therefore holds *within* a run (venue lookup by id, unique journal row) and not
    across runs — the place.py docstring used to claim otherwise (review X-8/X-N5).
    """
    venue = FakeVenue()
    for run, plan in (("run-a", "p1"), ("run-b", "p2")):
        _seed(engine, run, plan)
        place_approved(engine, venue, run, _plan(), _gate(), plan, "SOL-USDT-SWAP")
    ids = {row["cl_ord_id"] for row in _orders(engine)}
    assert len(ids) == 2


def test_the_adapter_contract_key_is_what_place_checks(engine: Engine) -> None:
    """Both adapters return `entry_id`. `place_approved` used to look for `entry`.

    The consequence was the dangerous direction: the order was live at the venue and the journal
    said REJECTED, so nothing would ever amend, cancel or reconcile it (F-138). The fake adapter
    agreed with the mistake because its fixture used the same wrong key.
    """
    import inspect

    from argus.exchange import fake, okx

    for module in (fake, okx):
        source = inspect.getsource(module.FakeExchange if module is fake else module.OKXAdapter)
        assert '"entry_id"' in source, f"{module.__name__} no longer returns entry_id"

    venue = FakeVenue(ids={"entry_id": "abc"})
    assert place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", "SOL-USDT-SWAP")
