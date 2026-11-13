"""The second opinion is only worth its latency if it can disagree. These cases check that."""

from __future__ import annotations

import pytest

from argus.llm.agents.consensus import Agreement, compare
from argus.llm.golden import CONSENSUS_CASES, Expectation, case_prompt, score_consensus
from argus.llm.schemas import TraderAnswer


def _case(name: str):  # noqa: ANN202 - a test helper; the type is the case
    return next(c for c in CONSENSUS_CASES if c.name == name)


def test_the_model_never_sees_the_primary_answer() -> None:
    """An anchored second opinion is a rubber stamp with extra latency.

    The whole design rests on the two answers being independent, so this pins the one thing that
    would silently destroy it: leaking the primary's plan into the prompt.
    """
    case = _case("primary_trades_into_chop")
    prompt, output_type = case_prompt(case)
    primary = case.payload["primary"]
    assert output_type is TraderAnswer
    assert str(primary.plan.stop) not in prompt
    assert str(primary.plan.entry_low) not in prompt
    assert "primary" not in prompt.lower()


def test_agreeing_with_a_bad_plan_is_scored_wrong() -> None:
    case = _case("primary_trades_into_chop")
    rubber_stamp = case.payload["primary"]  # the identical plan, i.e. total agreement
    assert not score_consensus(case, rubber_stamp).correct

    refusal = TraderAnswer(decision="no_trade", reason="range-bound, no structural edge")
    assert score_consensus(case, refusal).correct


def test_refusing_a_good_setup_is_also_scored_wrong() -> None:
    """The opposite failure, and the quieter one: a model that blocks everything.

    Without these cases a permanently pessimistic model would score perfectly on the vetoes and
    veto every real trade in production.
    """
    case = _case("clean_setup_both_want_it")
    refusal = TraderAnswer(decision="no_trade", reason="I would rather not")
    assert not score_consensus(case, refusal).correct
    assert score_consensus(case, case.payload["primary"]).correct


def test_two_refusals_agree_but_do_not_trade() -> None:
    case = _case("nothing_there_both_decline")
    both_out = TraderAnswer(decision="no_trade", reason="no edge")
    score = score_consensus(case, both_out)
    assert score.correct
    # `compare` must call this an agreement that does NOT trade; an agreement the run would act
    # on is a different outcome and the scorer distinguishes them.
    assert compare(case.payload["primary"], both_out).tradeable is False


def test_a_veto_counts_however_it_is_expressed() -> None:
    """Declining and reading the opposite direction both block the trade."""
    case = _case("primary_fights_the_regime")
    opposite = TraderAnswer(decision="no_trade", reason="the regime is against this short")
    assert score_consensus(case, opposite).correct
    assert Agreement.CONFLICT.value in case.payload["also_accept"]


@pytest.mark.parametrize("case", CONSENSUS_CASES)
def test_every_case_states_a_primary_answer(case) -> None:  # noqa: ANN001
    assert isinstance(case.payload["primary"], TraderAnswer)
    assert case.expect in {Expectation.AGREE, Expectation.ONE_SIDED, Expectation.CONFLICT}


def test_the_suite_can_fail_in_both_directions() -> None:
    """A suite of only vetoes rewards pessimism; a suite of only agreements rewards a stamp."""
    expects = [c.expect for c in CONSENSUS_CASES]
    assert expects.count(Expectation.AGREE) >= 2
    assert expects.count(Expectation.ONE_SIDED) >= 2
