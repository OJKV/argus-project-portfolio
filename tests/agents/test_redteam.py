"""The Red Team is judged on both sides of its verdict.

A red team that objects to everything is noise, and noise is ignored exactly when it matters. One
that agrees with everything is decoration. Both failures are tested, because either makes the
agent worthless in a different direction.
"""

from __future__ import annotations

import pytest

from argus.llm.golden import REDTEAM_CASES, Expectation, score_redteam
from argus.llm.schemas import RedTeamVerdict, TradePlan


def _verdict(agree: bool, objections: list[str] | None = None) -> RedTeamVerdict:
    return RedTeamVerdict(
        agree=agree, severity=0 if agree else 3, objections=objections or ["something is wrong"]
    )


def test_the_set_tests_agreeing_as_well_as_objecting() -> None:
    assert any(c.expect is Expectation.AGREE for c in REDTEAM_CASES)
    assert sum(c.expect is Expectation.OBJECT for c in REDTEAM_CASES) >= 4


def test_agreeing_with_a_sound_reading_is_correct() -> None:
    case = next(c for c in REDTEAM_CASES if c.expect is Expectation.AGREE)
    assert score_redteam(case, _verdict(True)).correct
    assert not score_redteam(case, _verdict(False, ["looks optimistic"])).correct


def test_an_objection_must_name_what_it_is_about() -> None:
    """ "The analysis seems optimistic" is a mood, not a finding, and nothing downstream can act
    on it."""
    case = next(c for c in REDTEAM_CASES if c.name == "ignored_hostile_funding")
    vague = score_redteam(case, _verdict(False, ["the reading seems too optimistic"]))
    assert not vague.correct

    specific = score_redteam(
        case, _verdict(False, ["funding is at the 96th percentile and is not mentioned"])
    )
    assert specific.correct


def test_a_verdict_without_objections_cannot_be_built() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RedTeamVerdict(agree=False, severity=3, objections=[])


@pytest.mark.parametrize("case", REDTEAM_CASES, ids=lambda c: c.name)
def test_every_case_renders_with_the_reading_under_review(case) -> None:
    """Through `case_prompt`, the one dispatch the eval and production share."""
    from argus.llm.golden import case_prompt

    reading = case.payload.get("plan") or case.payload["analysis"]
    text, output_type = case_prompt(case)
    assert "<analysis>" in text
    assert reading["symbol"] in text
    assert output_type is RedTeamVerdict


# --- Trader plans: what the live chain actually sends (L-4) ----------------------------------
PLAN_CASES = [c for c in REDTEAM_CASES if "plan" in c.payload]


def test_the_suite_measures_the_plans_the_live_chain_sends() -> None:
    """Since 2026-10-06 the Red Team reads the Trader's plan, but its suite only ever fed it
    analyst readings — so the severity that now halves or drops a live trade was measured on a
    different input. Both sides are needed: sound plans it must let through, and the three flaws
    the live chain relies on it to catch."""
    sound = [c for c in PLAN_CASES if c.expect is Expectation.AGREE]
    flawed = {c.name for c in PLAN_CASES if c.expect is Expectation.OBJECT}
    assert len(sound) >= 2, "a red team measured only on bad plans learns to object to all"
    assert {"plan_uncited_score", "plan_invented_level", "plan_ignored_contradiction"} <= flawed


@pytest.mark.parametrize("case", PLAN_CASES, ids=lambda c: c.name)
def test_every_plan_case_is_a_plan_the_red_team_would_really_see(case) -> None:
    """A real TradePlan, and one that clears the code-side evidence check in `vet()` — a flaw
    that code already refuses never reaches the Red Team, so measuring it there is pointless."""
    from argus.llm.evidence import unsupported_scores

    plan = TradePlan.model_validate(case.payload["plan"])
    assert unsupported_scores(plan, case.dossier()) == {}


@pytest.mark.parametrize("case", PLAN_CASES, ids=lambda c: c.name)
def test_plan_cases_are_scored_on_severity_both_ways(case) -> None:
    from argus.llm.agents.redteam import ACTIONABLE_SEVERITY

    needles = case.payload.get("must_mention") or ["the plan"]
    specific = [f"objection about {needles[0]}"]
    below = RedTeamVerdict(agree=False, severity=ACTIONABLE_SEVERITY - 1, objections=specific)
    at = RedTeamVerdict(agree=False, severity=ACTIONABLE_SEVERITY, objections=specific)
    if case.expect is Expectation.AGREE:
        assert score_redteam(case, below).correct and not score_redteam(case, at).correct
    else:
        assert score_redteam(case, at).correct and not score_redteam(case, below).correct


@pytest.mark.parametrize(
    "case", [c for c in REDTEAM_CASES if c.expect is Expectation.AGREE], ids=lambda c: c.name
)
def test_a_sound_reading_cites_only_what_the_dossier_shows(case) -> None:
    """An AGREE fixture that cites a field the model was never shown is not sound: a careful
    critic is right to object, and the miss is the fixture's, not the model's (the F-136 lesson).
    `multi_family_reading_matched_confidence` cited `chain.netflow7d$` from on-chain inputs the
    dossier renderer drops, so its "three families" were two."""
    from argus.llm.evidence import evidence_paths, leaf

    reading = case.payload.get("plan") or case.payload["analysis"]
    known = evidence_paths(case.dossier())
    cited = [path for paths in reading["evidence"].values() for path in paths]
    assert [path for path in cited if leaf(path) not in known] == []
