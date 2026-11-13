"""Risk Debate: it sees the book, and it can never ask for more."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.llm.agents.riskdebate import SIZE_FACTOR, apply_verdict, build_prompt
from argus.llm.golden import RISK_CASES, Expectation, score_risk
from argus.llm.schemas import RiskVerdict


def test_no_recommendation_can_increase_size() -> None:
    assert max(SIZE_FACTOR.values()) == 1.0
    with pytest.raises(ValidationError):
        RiskVerdict(recommendation="increase", rationale="x")


def test_the_factor_is_clamped_even_if_the_table_were_wrong() -> None:
    import argus.llm.agents.riskdebate as mod

    original = dict(mod.SIZE_FACTOR)
    try:
        mod.SIZE_FACTOR["proceed"] = 4.0
        assert apply_verdict(RiskVerdict(recommendation="proceed", rationale="ok")) == 1.0
    finally:
        mod.SIZE_FACTOR.clear()
        mod.SIZE_FACTOR.update(original)


def test_a_reduction_must_say_why() -> None:
    """A reduction nobody can explain cannot be reviewed, and cannot be learned from."""
    with pytest.raises(ValidationError, match="concern"):
        RiskVerdict(recommendation="reduce", concerns=[], rationale="felt risky")


def test_the_set_includes_the_answer_that_changes_nothing() -> None:
    """Proceeding is the normal answer; a reviewer that never gives it trims every trade."""
    assert any(c.expect is Expectation.PROCEED for c in RISK_CASES)
    assert any(c.expect is Expectation.DECLINE for c in RISK_CASES)


def test_the_right_call_scores_and_a_vague_one_does_not() -> None:
    case = next(c for c in RISK_CASES if c.name == "correlated_with_open_long")
    good = RiskVerdict(
        recommendation="reduce",
        concerns=["ETH and AVAX longs are open with high BTC beta"],
        rationale="correlated exposure",
    )
    assert score_risk(case, good).correct
    vague = RiskVerdict(recommendation="reduce", concerns=["feels risky"], rationale="caution")
    assert not score_risk(case, vague).correct


@pytest.mark.parametrize("case", RISK_CASES, ids=lambda c: c.name)
def test_every_case_renders(case) -> None:
    p = case.payload
    text = build_prompt(p["plan"], p["portfolio"], p["regime"], p["calendar"])
    assert "only ask for less" in text
    assert p["plan"]["symbol"] in text
