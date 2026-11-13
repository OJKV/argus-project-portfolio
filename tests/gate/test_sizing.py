"""Sizing tests — DECISION_LOGIC.md §5 worked example and §5.1 small-account steps."""

from decimal import Decimal

import pytest

from argus.gate.sizing import (
    InstrumentSpec,
    SizingInput,
    StepPolicy,
    size_position,
    stop_distance_pct,
)

ARB = InstrumentSpec(
    contract_value=Decimal("10"),
    lot_size=Decimal("1"),
    min_contracts=Decimal("1"),
    max_leverage=50,
    maint_margin_rate=Decimal("0.01"),
)
STEPS = (
    StepPolicy(0, 1.0, 10, 2, 0.25),
    StepPolicy(1, 2.0, 10, 2, 0.40),
    StepPolicy(2, 3.5, 10, 2, 0.50),
    StepPolicy(3, 5.0, 10, 5, 0.60),
)


def _inp(
    equity: float,
    steps: tuple[StepPolicy, ...] = STEPS,
    entry: float = 0.158,
    stop: float = 0.170,
    mark: float = 0.158,
    atr_pct: float = 3.1,
) -> SizingInput:
    return SizingInput(
        equity_usd=equity,
        entry=entry,
        stop=stop,
        side="short",
        mark=mark,
        atr_pct=atr_pct,
        regime_mult=1.0,
        protection_mult=1.0,
        spec=ARB,
        steps=steps,
    )


def test_worked_example_arb_short_2000_equity() -> None:
    """Equity $2,000, risk 1% = $20, stop 7.6% → notional ≈ $263, 13% of equity, leverage 2–3x."""
    res = size_position(_inp(2000))
    assert res.ok and res.step == 0
    # Placed risk (G-11): lot rounding only ever shrinks the $20 budget, by under one contract.
    assert 19.5 <= res.risk_usd <= 20.0
    assert res.stop_dist_pct == pytest.approx(0.0759, abs=1e-3)
    assert 240 <= res.notional_usd <= 265  # contract rounding pulls slightly below 263
    # G-18: `2 <= lev <= 3` hid G-12. Leverage is always the step minimum today because the
    # margin budget equals the notional cap; the spec's ~3x needs a margin-budget number nobody
    # has decided (G-12, open, design). Pinned exactly so the day it changes is visible.
    assert res.leverage == 2
    assert res.liq_dist_pct >= 2.5 * res.stop_dist_pct
    assert res.margin_usd == pytest.approx(res.notional_usd / res.leverage, rel=1e-6)


def test_small_account_escalates_steps() -> None:
    """$150 equity: step 0 notional (~$19) < $25 floor → step 1 (2% risk, ~$39)."""
    res = size_position(_inp(150))
    assert res.ok
    assert res.step == 1
    assert res.notional_usd >= 25.0
    assert 2.8 <= res.risk_usd <= 3.0  # the 2 % budget, less lot rounding (G-11)


def test_insufficient_balance_when_no_step_fits() -> None:
    res = size_position(_inp(5))
    assert not res.ok and res.reason == "INSUFFICIENT_BALANCE"


def test_stop_outside_atr_band_rejected() -> None:
    res = size_position(_inp(2000, atr_pct=1.0))  # stop = 7.6 x ATR → outside [1, 3.5]
    assert not res.ok and res.reason == "STOP_OUTSIDE_ATR_BAND"


def test_conservative_profile_has_no_step_3() -> None:
    res = size_position(_inp(150, steps=STEPS[:3]))
    assert res.ok and res.step in (1, 2)
    res_tiny = size_position(_inp(20, steps=STEPS[:3]))  # even 3.5% of $20 < $25 floor
    assert not res_tiny.ok and res_tiny.reason == "INSUFFICIENT_BALANCE"


def test_stop_side_validation() -> None:
    with pytest.raises(ValueError):
        stop_distance_pct(100, 110, "long")
    with pytest.raises(ValueError):
        stop_distance_pct(100, 90, "short")


def _reduced(equity: float, agent: float = 1.0, protection: float = 1.0) -> SizingInput:
    base = _inp(equity)
    return SizingInput(
        equity_usd=base.equity_usd,
        entry=base.entry,
        stop=base.stop,
        side=base.side,
        mark=base.mark,
        atr_pct=base.atr_pct,
        regime_mult=1.0,
        protection_mult=protection,
        agent_mult=agent,
        spec=base.spec,
        steps=base.steps,
    )


def test_a_reduction_is_not_undone_by_escalating_to_a_bigger_step() -> None:
    """G-1 / G-N2: $150, step 1 is the unreduced trade. Halving it must not climb to step 2.

    Before the fix the halved notional ($19.7) fell under the $25 floor, the loop escalated to
    step 2 (3.5 % risk) and delivered 87.5 % of the unreduced risk — the Risk Debate's `reduce`
    and the post-loss x0.5 bought nothing on a small account.
    """
    full = size_position(_reduced(150))
    assert full.ok and full.step == 1
    for kwargs in ({"agent": 0.5}, {"protection": 0.5}):
        halved = size_position(_reduced(150, **kwargs))
        if halved.ok:
            assert halved.step == full.step
            assert halved.risk_usd <= full.risk_usd * 0.5 + 1e-9
        else:
            assert halved.reason == "REDUCED_BELOW_MINIMUM"


def test_risk_is_what_the_placed_contracts_can_lose_not_the_budget() -> None:
    """G-11: when the notional cap binds, the trade risks less than its budget. The heat gate,
    R-multiples and gate 14 must see the risk of the order actually placed."""
    tight = SizingInput(
        equity_usd=2000,
        entry=100.0,
        stop=98.9,  # 1.1 % stop: $20 of risk would need $1,818 of notional
        side="long",
        mark=100.0,
        atr_pct=0.5,
        regime_mult=1.0,
        protection_mult=1.0,
        spec=InstrumentSpec(
            contract_value=Decimal("0.01"),
            lot_size=Decimal("1"),
            min_contracts=Decimal("1"),
            max_leverage=50,
            maint_margin_rate=Decimal("0.01"),
        ),
        steps=STEPS,
    )
    res = size_position(tight)
    assert res.ok and res.notional_usd <= 500.0  # capped at 25 % of equity
    placed = float(res.contracts * Decimal("0.01")) * abs(100.0 - 98.9)
    assert res.risk_usd == pytest.approx(placed)
    assert res.risk_usd < 20.0


def test_a_long_is_sized_like_a_short() -> None:
    """G-18: every sizing test was a short. Same distance, mirrored: same risk and size."""
    short = size_position(_inp(2000, entry=0.158, stop=0.170, mark=0.158))
    long_ = size_position(
        SizingInput(
            equity_usd=2000,
            entry=0.158,
            stop=0.146,
            side="long",
            mark=0.158,
            atr_pct=3.1,
            regime_mult=1.0,
            protection_mult=1.0,
            spec=ARB,
            steps=STEPS,
        )
    )
    assert long_.ok and long_.step == short.step
    assert long_.stop_dist_pct == pytest.approx(short.stop_dist_pct)
    assert long_.contracts == short.contracts
    assert long_.risk_usd == pytest.approx(short.risk_usd)


def test_a_reduction_on_a_large_account_halves_the_trade() -> None:
    full = size_position(_reduced(2000))
    halved = size_position(_reduced(2000, agent=0.5))
    assert full.ok and halved.ok and halved.step == full.step == 0
    assert halved.notional_usd == pytest.approx(full.notional_usd / 2, rel=0.05)
