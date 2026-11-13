"""The Regime agent can only ask for less risk.

This is the one agent whose output multiplies every other decision in the run, so the protection
is structural: there is no posture that means "more", and the function that applies it clamps
anyway. Two independent guards, because a single one is a single edit away from gone.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.features.regime import RegimeInputs, classify
from argus.llm.agents.regime import POSTURE_MULTIPLIER, apply_posture, build_prompt
from argus.llm.schemas import RegimeCall


def _computed(trend: str = "up"):
    if trend == "up":
        return classify(
            RegimeInputs(
                btc_close=100,
                btc_ema20=95,
                btc_ema50=90,
                btc_ema20_slope=0.5,
                btc_rv20_pct=40,
                breadth_20=62,
                next_event_hours=96,
            )
        )
    return classify(
        RegimeInputs(
            btc_close=100,
            btc_ema20=100.5,
            btc_ema50=99.8,
            btc_ema20_slope=0.01,
            btc_rv20_pct=25,
            breadth_20=48,
            next_event_hours=96,
        )
    )


def _call(**over) -> RegimeCall:
    base = {"label": "TREND_UP", "confidence": 3, "reasons": ["price over the 20 and 50"]}
    base.update(over)
    return RegimeCall(**base)


def test_no_posture_can_increase_risk() -> None:
    """Not "is rejected" — there is no value to express it with."""
    assert max(POSTURE_MULTIPLIER.values()) == 1.0
    with pytest.raises(ValidationError):
        _call(posture="aggressive")
    with pytest.raises(ValidationError):
        _call(posture="increase")


def test_normal_leaves_the_computed_risk_alone() -> None:
    computed = _computed("up")
    assert apply_posture(computed, _call(posture="normal")) == computed.risk_mult


def test_reduce_halves_it_and_stand_aside_stops_the_book() -> None:
    computed = _computed("up")
    assert apply_posture(computed, _call(posture="reduce")) == pytest.approx(
        computed.risk_mult * 0.5
    )
    assert apply_posture(computed, _call(posture="stand_aside")) == 0.0


def test_the_result_is_clamped_even_if_the_table_were_wrong() -> None:
    """Belt and braces: if a future edit admitted a multiplier above 1, this still would not
    return more risk than the classifier computed."""
    import argus.llm.agents.regime as mod

    original = dict(mod.POSTURE_MULTIPLIER)
    try:
        mod.POSTURE_MULTIPLIER["normal"] = 3.0
        computed = _computed("up")
        assert apply_posture(computed, _call(posture="normal")) == computed.risk_mult
    finally:
        mod.POSTURE_MULTIPLIER.clear()
        mod.POSTURE_MULTIPLIER.update(original)


def test_standing_the_book_aside_requires_conviction() -> None:
    """The most expensive request this agent can make should not come from a shrug."""
    with pytest.raises(ValidationError, match="confidence"):
        _call(posture="stand_aside", confidence=1)
    assert _call(posture="stand_aside", confidence=3).posture == "stand_aside"


def test_an_unknown_label_is_refused() -> None:
    with pytest.raises(ValidationError):
        _call(label="MOON")


def test_top_risks_stay_short() -> None:
    with pytest.raises(ValidationError):
        _call(top_risks=["a", "b", "c", "d"])


def test_the_prompt_carries_the_computed_verdict_and_forbids_more_risk() -> None:
    text = build_prompt(_computed("up"), {"btc_close": 100, "breadth_20": 62}, [])
    assert "TREND_UP" in text
    assert "only ask for less risk" in text
    assert "do not forecast" in text.lower()
