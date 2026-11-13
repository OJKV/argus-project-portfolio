"""Regime rule table and modifiers (DECISION_LOGIC.md §2)."""

from __future__ import annotations

import pytest

from argus.features.regime import RegimeInputs, apply_posture, classify

TREND_UP = dict(
    btc_close=76000.0,
    btc_ema20=74000.0,
    btc_ema50=70000.0,
    btc_ema20_slope=0.8,
    btc_rv20_pct=50.0,
    breadth_20=62.0,
    next_event_hours=72.0,
)


def _inputs(**over: object) -> RegimeInputs:
    return RegimeInputs.model_validate({**TREND_UP, **over})


def test_trend_up() -> None:
    view = classify(_inputs())
    assert view.label == "TREND_UP"
    assert view.risk_mult == 1.0 and not view.limit_only


def test_trend_down() -> None:
    view = classify(
        _inputs(
            btc_close=66000.0,
            btc_ema20=70000.0,
            btc_ema50=74000.0,
            btc_ema20_slope=-0.6,
            breadth_20=30.0,
        )
    )
    assert view.label == "TREND_DOWN" and view.risk_mult == 1.0


def test_weak_breadth_turns_a_btc_uptrend_into_chop() -> None:
    view = classify(_inputs(breadth_20=50.0))
    assert view.label == "CHOP"
    assert view.risk_mult == 0.5 and view.limit_only


@pytest.mark.parametrize("over", [{"next_event_hours": 6.0}, {"btc_rv20_pct": 95.0}])
def test_event_risk_wins_over_everything(over: dict[str, float]) -> None:
    view = classify(_inputs(**over))
    assert view.label == "EVENT_RISK"
    assert view.risk_mult == 0.4 and view.limit_only


def test_an_event_already_past_does_not_trigger_event_risk() -> None:
    assert classify(_inputs(next_event_hours=-2.0)).label == "TREND_UP"


@pytest.mark.parametrize(
    "missing", ["next_event_hours", "btc_rv20_pct", "breadth_20", "btc_ema20", "btc_ema20_slope"]
)
def test_missing_inputs_can_never_produce_a_full_size_regime(missing: str) -> None:
    # Without the calendar (or vol, breadth, trend) a trend cannot be confirmed: cap at CHOP.
    view = classify(_inputs(**{missing: None}))
    assert view.label == "CHOP"
    assert view.risk_mult <= 0.5 and view.limit_only
    assert any(missing in gap for gap in view.data_gaps)


def test_real_yield_tripwire_halves_longs_only() -> None:
    view = classify(_inputs(real_yield_10y=2.6))
    assert view.risk_mult_long == pytest.approx(0.5)
    assert view.risk_mult_short == pytest.approx(1.0)


def test_modifiers_compound_and_etf_outflows_hit_only_btc_and_eth_longs() -> None:
    view = classify(_inputs(real_yield_10y=2.6, stable_supply_7d_pct=-1.5, etf_5d_usd=-600e6))
    assert view.risk_mult_long == pytest.approx(0.5 * 0.7)
    assert view.risk_mult_for("SOL", "long") == pytest.approx(0.35)
    assert view.risk_mult_for("BTC", "long") == pytest.approx(0.5 * 0.7 * 0.7)
    assert view.risk_mult_for("ETH", "short") == pytest.approx(1.0)
    assert len(view.modifiers) == 3


def test_modifiers_scale_the_regime_base_multiplier() -> None:
    view = classify(_inputs(breadth_20=50.0, real_yield_10y=2.6))  # CHOP
    assert view.risk_mult_for("SOL", "long") == pytest.approx(0.25)


def test_missing_macro_is_a_gap_not_a_modifier() -> None:
    view = classify(_inputs())
    assert view.modifiers == []
    assert any("real_yield_10y" in gap for gap in view.data_gaps)


def test_posture_from_the_llm_can_only_reduce_risk() -> None:
    view = classify(_inputs())
    assert apply_posture(view, 0.5).risk_mult == pytest.approx(0.5)
    assert apply_posture(view, 3.0).risk_mult == pytest.approx(1.0)  # never raised
    assert apply_posture(view, -1.0).risk_mult == pytest.approx(0.0)  # floored, not inverted
    assert view.risk_mult == pytest.approx(1.0)  # the original view is untouched
    twice = apply_posture(apply_posture(view, 0.5), 1.0)
    assert twice.risk_mult == pytest.approx(0.5)


@pytest.mark.parametrize("posture", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_posture_shrinks_to_zero_instead_of_poisoning_the_multiplier(
    posture: float,
) -> None:
    """D-18b: min(max(nan, 0), 1) is nan, so a NaN posture made risk_mult NaN for every plan.
    An unreadable shrink request fails closed: no new risk this run."""
    view = apply_posture(classify(_inputs()), posture)
    assert view.risk_mult == 0.0
