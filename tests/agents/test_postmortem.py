"""The post-mortem's failure mode is hindsight, so these tests are about the verdict
disagreeing with the PnL."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.features.trade_flags import compute_trade_flags
from argus.llm.agents.postmortem import build_prompt, outcome_bias
from argus.llm.golden import POSTMORTEM_CASES, Expectation, score_postmortem
from argus.llm.schemas import PostMortem


def _report(**over: object) -> PostMortem:
    base = {
        "process_verdict": "sound",
        "would_take_again": True,
        "entry_timing": "good",
        "sizing": "right",
        "stop_placement": "right",
        "missed_signals": [],
        "do_differently": "",
        "feedback": "The thesis held; price reached the stop first.",
    }
    return PostMortem(**{**base, **over})  # type: ignore[arg-type]


def test_a_flawed_process_must_say_what_to_change() -> None:
    with pytest.raises(ValidationError):
        _report(process_verdict="flawed", would_take_again=False, do_differently="  ")


def test_a_sound_loss_is_not_flagged_as_biased() -> None:
    # The whole point: -1R with an intact thesis should pass through clean.
    assert outcome_bias(_report(), -1.0) is None


def test_contradiction_between_verdict_and_commitment_is_flagged() -> None:
    assert outcome_bias(_report(process_verdict="flawed", do_differently="x"), 2.1) is not None
    assert outcome_bias(_report(would_take_again=False), -1.0) is not None


def test_a_plan_defect_is_a_flawed_loss_without_a_missed_signal() -> None:
    """A stop inside ordinary noise is a defect with nothing missed at entry.

    An earlier version of `outcome_bias` demanded a missed signal from every flawed loss and
    accused two correct answers of hindsight. Kept as a regression: being right about the stop is
    not evidence of grading the outcome.
    """
    honest = _report(
        process_verdict="flawed",
        would_take_again=False,
        stop_placement="too_tight",
        missed_signals=[],
        do_differently="Place the stop at least 1x ATR from entry.",
    )
    assert outcome_bias(honest, -1.0) is None


def test_flags_expose_the_arithmetic_the_model_kept_missing() -> None:
    case = next(c for c in POSTMORTEM_CASES if c.name == "chased_the_entry")
    flags = compute_trade_flags(case.payload["plan"], case.payload["outcome"])
    # 186.2 filled against a planned 178.5 — the subtraction v1 of the prompt never made.
    assert flags["entry_slippage_pct"] == pytest.approx(4.31, abs=0.01)
    assert flags["hit_time_stop"] is True

    tight = next(c for c in POSTMORTEM_CASES if c.name == "stop_inside_ordinary_noise")
    assert (
        compute_trade_flags(tight.payload["plan"], tight.payload["outcome"])["stop_distance_atr"]
        < 0.5
    )


def test_slippage_is_signed_against_the_trader_on_both_sides() -> None:
    plan = {"side": "short", "entry": 100.0, "stop": 105.0}
    # A short filled lower than planned is worse for the trader, so it reads positive.
    assert compute_trade_flags(plan, {"fill_price": 96.0})["entry_slippage_pct"] == 4.0


def test_prompt_carries_the_computed_flags() -> None:
    case = POSTMORTEM_CASES[0]
    prompt = build_prompt(case.payload["plan"], case.payload["trail"], case.payload["outcome"])
    assert "stop_distance_atr" in prompt
    assert "<flags>" in prompt


def test_scorer_checks_the_commitment_not_just_the_verdict() -> None:
    case = next(c for c in POSTMORTEM_CASES if c.expect is Expectation.SOUND)
    assert score_postmortem(case, _report()).correct
    # Right verdict, wrong commitment — a model that says "sound, but never again" has not
    # actually decided anything.
    assert not score_postmortem(case, _report(would_take_again=False)).correct
