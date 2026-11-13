"""The Reviewer's golden cases: hold, trim, close, tighten.

Every case is a decision a human trader would make quickly and defend easily. If a model
disagrees with one of these for a good reason, the case is wrong — but "I would not enter here
today" is never a good reason, and that confusion is the whole point of testing this agent.
"""

from __future__ import annotations

from typing import Any

import pytest

from argus.llm.golden import REVIEW_CASES, score_review
from argus.llm.schemas import ReviewDecision


def _decision(action: str, **over: Any) -> ReviewDecision:
    base: dict[str, Any] = {"action": action, "side": "long", "rationale": "because"}
    if action == "MOVE_STOP":
        base.update(current_stop=100.0, new_stop=105.0)
    if action == "TRIM":
        base.update(trim_pct=50)
    base.update(over)
    return ReviewDecision(**base)


def test_the_set_covers_the_decisions_that_cost_money() -> None:
    names = {c.name for c in REVIEW_CASES}
    assert {
        "invalidation_hit_at_a_loss",
        "working_and_intact",
        "tp1_hit_thesis_intact",
        "time_stop_breached_flat",
        "drawdown_thesis_intact",
        "flush_against_intact_thesis",
        "spread_blown_out",
        "stale_price_feed",
    } <= names


def test_every_case_states_the_expected_action_and_why() -> None:
    for case in REVIEW_CASES:
        assert case.expect.value in ("HOLD", "CLOSE", "TRIM", "MOVE_STOP"), case.name
        assert case.why
        assert case.payload["position"]["symbol"]


def test_the_right_action_scores_and_a_wrong_one_does_not() -> None:
    case = next(c for c in REVIEW_CASES if c.name == "invalidation_hit_at_a_loss")
    assert score_review(case, _decision("CLOSE")).correct
    assert not score_review(case, _decision("HOLD")).correct


def test_holding_a_working_trade_is_correct() -> None:
    case = next(c for c in REVIEW_CASES if c.name == "working_and_intact")
    assert score_review(case, _decision("HOLD")).correct
    assert not score_review(case, _decision("CLOSE")).correct


@pytest.mark.parametrize("case", REVIEW_CASES, ids=lambda c: c.name)
def test_every_review_case_renders_a_prompt(case) -> None:
    from argus.llm.agents.reviewer import build_prompt

    text = build_prompt(dossier=case.dossier(), **case.payload)
    assert case.payload["position"]["symbol"] in text
    assert "never widen a stop" in text.lower()
    assert "use your judgement" in text.lower()


def test_a_widened_stop_cannot_even_be_expressed() -> None:
    """The prompt asks; the schema enforces. A reviewer that can widen a stop can turn a small
    loss into a large one, so it is refused at construction, not at review time."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _decision("MOVE_STOP", current_stop=100.0, new_stop=95.0)


def test_the_prompt_leaves_room_to_reason() -> None:
    """A reviewer that only recognises listed cases is lost the first time reality does
    something new, so the prompt must say so out loud rather than imply it."""
    from argus.llm.prompts import load

    body = load("reviewer").body.lower()
    assert "not a complete list" in body
    assert "use your judgement" in body


def test_the_matrix_is_covered_by_cases() -> None:
    """Every decision the matrix can produce has at least one case behind it."""
    actions = {c.expect.value for c in REVIEW_CASES}
    assert actions == {"HOLD", "CLOSE", "TRIM", "MOVE_STOP"}
    assert len(REVIEW_CASES) >= 20


def test_the_reviewer_is_kept_off_the_traders_family() -> None:
    """`reviewer: trader` is an adversarial pair, so the plan carries the family that wrote it.

    review.py never passed `used_families`, so the Reviewer that manages live stops could come
    from the Trader's own lab — an echo, not a review (L-13). The journaled plan now names that
    family; the agent reads it from there, and keeps it out of the prompt.
    """
    from argus.llm.agents import reviewer

    case = REVIEW_CASES[0]
    seen: dict[str, Any] = {}

    class _Router:
        def call(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    payload = dict(case.payload)
    payload["plan"] = {**case.payload["plan"], "trader_family": "google"}
    reviewer.review(_Router(), "r1", dossier=case.dossier(), **payload)  # type: ignore[arg-type]
    assert seen["used_families"] == {"trader": "google"}
    assert "google" not in seen["prompt"]

    # A plan journaled before the family was recorded: nothing to avoid, nothing invented.
    seen.clear()
    reviewer.review(_Router(), "r1", dossier=case.dossier(), **case.payload)  # type: ignore[arg-type]
    assert seen["used_families"] is None
