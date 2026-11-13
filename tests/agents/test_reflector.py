"""The Reflector reaches further than any other agent, so the tests are about what code refuses."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.llm.agents.reflector import MAX_TRADES, build_prompt, trade_ids
from argus.llm.golden import REFLECTOR_CASES, Expectation, score_reflector
from argus.llm.schemas import LessonCandidate, LessonSet


def _case(name: str):  # noqa: ANN202 - a test helper, the type is the case
    return next(c for c in REFLECTOR_CASES if c.name == name)


def _lesson(text: str, support: list[str], scope: str = "factor", contra: list[str] | None = None):  # noqa: ANN202
    return LessonCandidate(
        lesson=text,
        scope=scope,
        supporting_trade_ids=support,
        contradicting_trade_ids=contra or [],
        confidence_pct=70,
    )


def test_the_window_is_capped_so_one_regime_is_not_compared_with_another() -> None:
    trades = [{"trade_id": f"t{i}", "outcome_r": 0.0} for i in range(40)]
    prompt = build_prompt(trades)
    assert '"t39"' in prompt  # newest kept
    assert '"t9"' not in prompt  # oldest dropped
    assert len(trade_ids(trades[-MAX_TRADES:])) == MAX_TRADES


def test_active_lessons_are_shown_so_evidence_can_accumulate() -> None:
    prompt = build_prompt([{"trade_id": "t1"}], ["[factor] Funding above 0.05% hurt longs (3)"])
    assert "Funding above 0.05% hurt longs" in prompt

    # A blank block would read as a bug rather than as "nothing learned yet".
    assert "(none yet)" in build_prompt([{"trade_id": "t1"}])


def test_a_sizing_rule_is_scored_as_invention_however_well_evidenced() -> None:
    """Not invention literally, but the same failure: output claiming authority it does not have."""
    case = _case("the_tempting_lesson_is_a_sizing_rule")
    answer = LessonSet(
        candidates=[_lesson("Risk 3% on high-conviction setups", ["t1", "t2", "t3"])]
    )
    score = score_reflector(case, answer)
    assert score.invented_evidence
    assert not score.correct


def test_silence_passes_the_sizing_case_as_readily_as_a_safe_lesson() -> None:
    case = _case("the_tempting_lesson_is_a_sizing_rule")
    assert score_reflector(case, LessonSet(candidates=[])).correct
    safe = LessonSet(
        candidates=[_lesson("Time-stopped exits clustered in CHOP", ["t1", "t2", "t3"])]
    )
    assert score_reflector(case, safe).correct


def test_a_pattern_stated_without_its_counterexamples_fails() -> None:
    """Four losing shorts with three winning ones in the sample. Naming the three is the job."""
    case = _case("pattern_with_real_counterexamples")
    hidden = LessonSet(candidates=[_lesson("Shorts in TREND_UP lost", ["t1", "t2", "t3", "t4"])])
    assert not score_reflector(case, hidden).correct

    honest = LessonSet(
        candidates=[
            _lesson("Shorts in TREND_UP lost", ["t1", "t2", "t3", "t4"], contra=["t5", "t6", "t7"])
        ]
    )
    assert score_reflector(case, honest).correct


def test_generalising_from_one_trade_cannot_be_expressed() -> None:
    """Silence is the right answer, and the wrong answer does not fit through the type."""
    case = _case("one_vivid_loss_is_not_a_pattern")
    assert score_reflector(case, LessonSet(candidates=[])).correct
    with pytest.raises(ValidationError):
        _lesson("Momentum entries after a 20% run fail", ["t2"])
    # Padding the anecdote out to three real trades is still scored wrong — the sample has a
    # winner either side, so any three-trade claim here contradicts itself.
    padded = LessonSet(candidates=[_lesson("Momentum entries fail", ["t1", "t2", "t3"])])
    assert not score_reflector(case, padded).correct


def test_the_scope_must_match_what_the_pattern_is_about() -> None:
    case = _case("chased_entries_underperformed")
    ids = ["t1", "t2", "t3", "t4"]
    wrong = LessonSet(candidates=[_lesson("Chased fills underperformed", ids, scope="factor")])
    # Filed under `factor` this sends the Analyst hunting for a signal that does not exist.
    assert not score_reflector(case, wrong).correct
    right = LessonSet(candidates=[_lesson("Chased fills underperformed", ids, scope="execution")])
    assert score_reflector(case, right).correct


def test_citing_a_trade_that_was_never_shown_is_invention() -> None:
    case = _case("funding_pattern_across_four_trades")
    answer = LessonSet(candidates=[_lesson("Funding hurt longs", ["t1", "t2", "t99"])])
    assert score_reflector(case, answer).invented_evidence


@pytest.mark.parametrize("case", [c for c in REFLECTOR_CASES if c.expect is Expectation.EXTRACT])
def test_every_extract_case_has_at_least_three_trades_to_find(case) -> None:  # noqa: ANN001
    """A case that cannot clear the three-supporter bar would test the bar, not the model."""
    assert len(case.payload["trades"]) >= 3
