"""Demo-only target repair (owner, 2026-10-26): a plan whose targets sit too close is moved out to
the profile's R:R floor instead of rejected. Live never repairs."""

from __future__ import annotations

import dataclasses

import pytest

from argus.gate.limits import DEMO_EXPLORATION, LIVE_CONSERVATIVE
from argus.gate.repair import repair_stop, repair_targets
from argus.gate.rules import Target, _blended_rr, _rr
from tests.gate.test_rules import _plan


def _r1(plan, entry: float) -> float:  # noqa: ANN001
    return _rr(entry, plan.stop, plan.targets[0].price, plan.side)


def test_live_never_repairs() -> None:
    plan = _plan(targets=[Target(100.9, 50), Target(102.0, 50)])  # entry 100, stop 94: 0.15R
    repaired, note = repair_targets(plan, LIVE_CONSERVATIVE, mark=100.0)
    assert repaired is plan and note is None


def test_demo_moves_close_targets_out_to_the_floor_and_keeps_everything_else() -> None:
    plan = _plan(targets=[Target(100.9, 50), Target(102.0, 50)])
    repaired, note = repair_targets(plan, DEMO_EXPLORATION, mark=100.0)
    entry = plan.entry
    assert _r1(repaired, entry) >= DEMO_EXPLORATION.min_rr_tp1
    assert _blended_rr(repaired, entry) >= DEMO_EXPLORATION.min_rr_blended
    assert repaired.stop == plan.stop and repaired.side == plan.side
    assert [t.close_pct for t in repaired.targets] == [50, 50]
    prices = [t.price for t in repaired.targets]
    assert prices == sorted(prices)
    assert note is not None and "repaired" in note


def test_demo_leaves_a_plan_that_already_clears_the_floor_alone() -> None:
    plan = _plan(targets=[Target(107.2, 50), Target(117.4, 50)])
    repaired, note = repair_targets(plan, DEMO_EXPLORATION, mark=100.0)
    assert repaired is plan and note is None


@pytest.mark.parametrize("side", ["long", "short"])
def test_repair_works_for_both_sides_and_a_single_target(side: str) -> None:
    stop = 94.0 if side == "long" else 106.0
    near = 100.6 if side == "long" else 99.4
    plan = _plan(side=side, stop=stop, targets=[Target(near, 100)])
    repaired, _ = repair_targets(plan, DEMO_EXPLORATION, mark=100.0)
    assert _r1(repaired, plan.entry) >= DEMO_EXPLORATION.min_rr_blended


# --- stop repair (owner, 2026-10-29) --------------------------------------------------------------
# entry 100, ATR 1.4% -> band [1.4%, 4.9%]; a 6% stop (94) is outside it, the regression seen
# on 2026-10-28 (Trader stops 4.9-6.6% against a ~4.5-5% ceiling).


def _wide(side: str = "long", **market: float) -> tuple:  # noqa: ANN001
    from tests.gate.test_rules import _market

    stop = 94.0 if side == "long" else 106.0
    swing = {"swing_low_20": 96.0} if side == "long" else {"swing_high_20": 104.0}
    targets = None if side == "long" else [Target(90.0, 50), Target(80.0, 50)]
    plan = _plan(side=side, stop=stop, **({"targets": targets} if targets else {}))
    return plan, _market(atr_pct=1.4, **{**swing, **market})


def test_live_never_repairs_a_wide_stop() -> None:
    plan, market = _wide()
    assert repair_stop(plan, LIVE_CONSERVATIVE, market) == (plan, None)
    assert "stop 6.00% outside" in evaluate_reason(plan, market, LIVE_CONSERVATIVE)


@pytest.mark.parametrize("side", ["long", "short"])
def test_demo_tightens_a_wide_stop_into_the_band_and_the_gate_passes_gate_3(side: str) -> None:
    plan, market = _wide(side)
    repaired, note = repair_stop(plan, DEMO_EXPLORATION, market)
    dist = abs(repaired.entry - repaired.stop) / repaired.entry * 100
    assert dist < abs(plan.entry - plan.stop) / plan.entry * 100  # only ever tighter
    assert 1.0 * 1.4 <= dist <= 3.5 * 1.4
    assert repaired.targets == plan.targets and repaired.side == plan.side
    assert note is not None and "stop repaired" in note
    assert evaluate_reason(repaired, market, DEMO_EXPLORATION).startswith("PASS")


def test_demo_does_not_repair_when_the_band_edge_is_inside_structure() -> None:
    plan, market = _wide(swing_low_20=94.5)  # swing at 5.5%: a 4.83% stop would sit above it
    assert repair_stop(plan, DEMO_EXPLORATION, market) == (plan, None)


def test_demo_never_widens_a_stop_that_is_too_tight() -> None:
    from tests.gate.test_rules import _market

    plan = _plan(stop=99.5)  # 0.5% < 1.0 x ATR
    assert repair_stop(plan, DEMO_EXPLORATION, _market(atr_pct=1.4)) == (plan, None)


def evaluate_reason(plan, market, limits) -> str:  # noqa: ANN001
    from argus.gate.rules import evaluate
    from tests.gate.test_rules import NOW, STEPS, _portfolio

    gate = evaluate(plan, market, _portfolio(), STEPS, NOW, limits=limits)
    return " ".join(f"{c.verdict} {c.detail}" for c in gate.reasons if c.gate == 3)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, 0.0])
def test_demo_leaves_the_plan_alone_on_an_unusable_atr(bad: float | None) -> None:
    plan, market = _wide()
    market = dataclasses.replace(market, atr_pct=bad)
    assert repair_stop(plan, DEMO_EXPLORATION, market) == (plan, None)


def test_demo_market_entry_is_measured_from_the_mark() -> None:
    plan, market = _wide()
    plan = dataclasses.replace(plan, entry_kind="market")
    repaired, _ = repair_stop(plan, DEMO_EXPLORATION, dataclasses.replace(market, mark=100.5))
    assert abs(100.5 - repaired.stop) / 100.5 * 100 <= 3.5 * 1.4
