"""Turning an approved plan into an order.

The non-negotiable (AGENTS.md rule 4) is that a position never exists without an exchange-side
stop. The way to guarantee that is to make an order without one impossible to construct, rather
than to remember to attach one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from argus.execution.orders import UnprotectedOrderError, build_entry_order
from argus.gate.rules import FactorScores, PlanInput, Target
from argus.gate.sizing import SizingResult

SIZED = SizingResult(
    ok=True,
    reason="",
    step=0,
    risk_usd=50.0,
    stop_dist_pct=3.0,
    notional_usd=1650.0,
    leverage=2,
    margin_usd=825.0,
    contracts=Decimal("1.1"),
    liq_dist_pct=40.0,
)


def _plan(**over: object) -> PlanInput:
    base: dict[str, object] = {
        "symbol": "BTC",
        "side": "long",
        "entry_kind": "limit",
        "entry_low": 104_100.0,
        "entry_high": 104_900.0,
        "stop": 101_500.0,
        "targets": [Target(109_500.0, 50), Target(116_200.0, 50)],
        "time_stop_hours": 120,
        "scores": FactorScores(0, 0, 0, 1, 2),
    }
    base.update(over)
    return PlanInput(**base)  # type: ignore[arg-type]


def test_the_order_carries_the_stop_as_an_attached_algo() -> None:
    order = build_entry_order(_plan(), SIZED, "run-1", "BTC-USDT-SWAP")
    stops = [a for a in order["attach_algos"] if a["kind"] == "sl"]
    assert len(stops) == 1
    assert stops[0]["trigger_px"] == Decimal("101500.0")


def test_an_order_without_a_stop_cannot_be_built() -> None:
    """Not "is rejected later" — cannot be constructed at all."""
    with pytest.raises(UnprotectedOrderError):
        build_entry_order(_plan(stop=0.0), SIZED, "run-1", "BTC-USDT-SWAP")


def test_a_stop_on_the_wrong_side_is_refused() -> None:
    with pytest.raises(UnprotectedOrderError):
        build_entry_order(_plan(stop=110_000.0), SIZED, "run-1", "BTC-USDT-SWAP")
    with pytest.raises(UnprotectedOrderError):
        build_entry_order(
            _plan(side="short", stop=100_000.0, targets=[Target(95_000.0, 100)]),
            SIZED,
            "run-1",
            "BTC-USDT-SWAP",
        )


def test_the_size_comes_from_the_gate_not_the_plan() -> None:
    """The model never sizes anything; the number must be the one sizing produced."""
    order = build_entry_order(_plan(), SIZED, "run-1", "BTC-USDT-SWAP")
    assert order["size"] == Decimal("1.1")
    assert order["leverage"] == 2


def test_an_unapproved_sizing_is_refused() -> None:
    bad = SizingResult(
        ok=False,
        reason="insufficient balance",
        step=0,
        risk_usd=0.0,
        stop_dist_pct=0.0,
        notional_usd=0.0,
        leverage=0,
        margin_usd=0.0,
        contracts=Decimal("0"),
        liq_dist_pct=0.0,
    )
    with pytest.raises(UnprotectedOrderError):
        build_entry_order(_plan(), bad, "run-1", "BTC-USDT-SWAP")


def test_the_first_target_is_attached_and_the_rest_are_left_to_the_reviewer() -> None:
    """OKX attaches one TP to an entry. The second target is the Reviewer's job, so attaching
    only TP1 keeps the exchange state and our intention in agreement."""
    order = build_entry_order(_plan(), SIZED, "run-1", "BTC-USDT-SWAP")
    tps = [a for a in order["attach_algos"] if a["kind"] == "tp"]
    assert len(tps) == 1
    assert tps[0]["trigger_px"] == Decimal("109500.0")


def test_the_order_is_idempotent_across_retries() -> None:
    first = build_entry_order(_plan(), SIZED, "run-1", "BTC-USDT-SWAP")
    second = build_entry_order(_plan(), SIZED, "run-1", "BTC/USDT:USDT")
    assert first["cl_ord_id"] == second["cl_ord_id"]


def test_a_limit_entry_prices_at_the_band_edge_that_favours_us() -> None:
    long_order = build_entry_order(_plan(), SIZED, "run-1", "BTC-USDT-SWAP")
    assert long_order["price"] == Decimal("104100.0")  # buy at the low edge

    short = _plan(side="short", stop=110_000.0, targets=[Target(99_000.0, 100)])
    short_order = build_entry_order(short, SIZED, "run-1", "BTC-USDT-SWAP")
    assert short_order["price"] == Decimal("104900.0")  # sell at the high edge


def test_a_market_entry_carries_no_price() -> None:
    order = build_entry_order(_plan(entry_kind="market"), SIZED, "run-1", "BTC-USDT-SWAP")
    assert order["order_type"] == "market"
    assert order.get("price") is None
