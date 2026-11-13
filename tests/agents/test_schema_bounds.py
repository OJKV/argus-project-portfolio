"""Free text from the Reviewer, Analyst and Red Team is length-bounded (Muse P2).

Unbounded prose is journaled and read back into later prompts, so an oversize answer must fail
validation (and be repaired or discarded) rather than be stored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.llm.schemas import AnalystView, RedTeamVerdict, ReviewDecision

HUGE = "x" * 5000
SCORES = {"fundamental": 0, "onchain": 0, "macro": 0, "catalyst": 0, "technical": 0}


def test_review_rationale_is_bounded() -> None:
    with pytest.raises(ValidationError):
        ReviewDecision(action="HOLD", side="long", rationale=HUGE)


def test_red_team_objections_are_bounded() -> None:
    with pytest.raises(ValidationError):
        RedTeamVerdict(agree=False, severity=3, objections=[HUGE])
    with pytest.raises(ValidationError):
        RedTeamVerdict(agree=False, severity=3, objections=["x"] * 500)


def test_analyst_free_text_is_bounded() -> None:
    with pytest.raises(ValidationError):
        AnalystView(symbol="BTC", scores=SCORES, summary=HUGE)
    with pytest.raises(ValidationError):
        AnalystView(symbol="BTC", scores=SCORES, data_gaps=[HUGE])
    with pytest.raises(ValidationError):
        AnalystView(symbol="BTC", scores=SCORES, evidence={"technical": [HUGE]})


def test_an_honest_answer_still_validates() -> None:
    AnalystView(symbol="BTC", scores=SCORES, summary="calm", data_gaps=["no onchain"])
    RedTeamVerdict(agree=True, severity=1, objections=["crowded"])
