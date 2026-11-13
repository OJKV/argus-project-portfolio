"""The Catalyst Extractor's job is restraint, so the tests are mostly about what it must not do."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.llm.agents.catalyst import build_prompt, evidence_block, filter_sourced, sources
from argus.llm.golden import CATALYST_CASES, Expectation, catalyst_evidence, score_catalyst
from argus.llm.schemas import CatalystCandidate, CatalystList


def _candidate(**over: object) -> CatalystCandidate:
    base = {
        "symbol": "ARB",
        "headline": "Arbitrum to unlock 92.6M ARB tokens on 2026-10-16",
        "kind": "unlock",
        "when": "2026-10-16",
        "source": "theblock",
        "impact": "high",
    }
    return CatalystCandidate(**{**base, **over})  # type: ignore[arg-type]


def test_when_must_be_an_iso_date_or_the_word_unknown() -> None:
    assert _candidate(when="unknown").when == "unknown"
    for vague in ("next week", "October", "soon", "2026-13-01"):
        with pytest.raises(ValidationError):
            _candidate(when=vague)


def test_a_candidate_must_name_a_source() -> None:
    with pytest.raises(ValidationError):
        _candidate(source="  ")


def test_unsourced_candidates_are_separated_from_cited_ones() -> None:
    evidence = catalyst_evidence(CATALYST_CASES[0])
    cited, invented = filter_sourced(
        [_candidate(), _candidate(source="a-newsletter-i-remember")], evidence
    )
    assert [c.source for c in cited] == ["theblock"]
    assert [c.source for c in invented] == ["a-newsletter-i-remember"]


def test_evidence_carries_the_publisher_the_model_must_copy() -> None:
    case = CATALYST_CASES[0]
    rendered = evidence_block([case.dossier()])
    assert 'src="theblock"' in rendered
    assert sources(rendered) == {"theblock"}


def test_prompt_states_todays_date_so_past_events_are_recognisable() -> None:
    prompt = build_prompt([CATALYST_CASES[0].dossier()])
    assert "Today is " in prompt
    assert "ARB" in prompt


def test_scorer_rejects_an_invented_date_for_an_undated_event() -> None:
    case = next(c for c in CATALYST_CASES if c.name == "undated_unlock_must_stay_unknown")
    kept = CatalystList(candidates=[_candidate(source="decrypt", when="unknown")])
    honest = score_catalyst(case, kept)
    assert honest.correct and not honest.invented_evidence

    guessed = score_catalyst(
        case, CatalystList(candidates=[_candidate(source="decrypt", when="2026-10-16")])
    )
    assert not guessed.correct
    # A date supplied from memory is invention, not a near miss — it is the failure that makes
    # every downstream time stop wrong while looking entirely plausible.
    assert guessed.invented_evidence


def test_scorer_wants_silence_on_the_nothing_cases() -> None:
    case = next(c for c in CATALYST_CASES if c.expect is Expectation.NOTHING)
    assert score_catalyst(case, CatalystList(candidates=[])).correct
    assert not score_catalyst(case, CatalystList(candidates=[_candidate()])).correct
