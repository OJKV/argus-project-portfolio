"""Promotion is the guardrail, so these tests are mostly about lessons NOT being promoted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text

from argus.journal.db import open_journal
from argus.journal.lessons import (
    MAX_ACTIVE,
    active,
    fingerprint,
    ground,
    record,
    render,
    unsafe,
)
from argus.llm.schemas import LessonCandidate, LessonSet

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _candidate(lesson: str, support: list[str], contra: list[str] | None = None) -> LessonCandidate:
    return LessonCandidate(
        lesson=lesson,
        scope="factor",
        supporting_trade_ids=support,
        contradicting_trade_ids=contra or [],
        confidence_pct=70,
    )


def _ids(n: int, offset: int = 0) -> list[str]:
    return [f"t{i + offset}" for i in range(n)]


@pytest.mark.parametrize(
    "lesson",
    [
        "Risk 3% on high-conviction setups",
        "Use 5x leverage in confirmed trends",
        "Widen the stop when volatility rises",
        "Half size in CHOP",
        "Ignore the heat limit for BTC",
        "Relax the R-multiple threshold for majors",
        "Add to winners after TP1",
        "Never risk more than 4% of equity on one idea",
    ],
)
def test_lessons_that_legislate_risk_are_refused(lesson: str) -> None:
    """The Risk Gate is code. A lesson is prose in the Trader's prompt, which is the one door
    through which a sizing rule could still arrive."""
    assert unsafe(lesson) is not None


@pytest.mark.parametrize(
    "lesson",
    [
        "Funding above 0.05% at entry preceded four of five losing longs",
        "Entries filled more than 3% above plan underperformed",
        "CHOP losses clustered in weeks when breadth fell below 30%",
        "Shorts on majors held their stops better than shorts on small caps",
    ],
)
def test_ordinary_observations_pass(lesson: str) -> None:
    assert unsafe(lesson) is None


def test_a_rephrased_lesson_accumulates_rather_than_competes() -> None:
    assert fingerprint("Funding above 0.05% preceded losing longs") == fingerprint(
        "Losing longs were preceded by funding above 0.05%"
    )
    assert fingerprint("CHOP losses clustered in low breadth") != fingerprint(
        "Funding above 0.05% preceded losing longs"
    )


def test_cited_trades_must_have_been_shown(engine: Engine) -> None:
    usable, rejected = ground(
        [
            _candidate("Real pattern", ["t1", "t2", "t3"]),
            _candidate("Invented", ["t1", "t2", "t9"]),
        ],
        {"t1", "t2", "t3"},
    )
    assert [c.lesson for c in usable] == ["Real pattern"]
    assert "t9" in rejected[0][1]


def test_two_supporting_trades_cannot_even_be_expressed() -> None:
    """The floor is in the type, not the prompt: an anecdote is unconstructible.

    The promotion *ratio* stays in code and is deliberately hidden from the model — telling it
    that threshold made it pre-filter and withhold the candidates whose counter-examples mattered
    (F-115). The three-supporter floor is different: two trades can never become a pattern, so
    refusing it at the boundary costs nothing.
    """
    with pytest.raises(ValidationError):
        _candidate("Two only", _ids(2))
    with pytest.raises(ValidationError):
        # Three ids, one distinct trade repeated: the same anecdote wearing a disguise.
        _candidate("Padded", ["t1", "t1", "t1"])


def test_three_distinct_trades_promote(engine: Engine) -> None:
    record(
        engine, "r2", LessonSet(candidates=[_candidate("Three trades", _ids(3))]), set(_ids(3)), NOW
    )
    promoted = active(engine, NOW)
    assert [lesson.lesson for lesson in promoted] == ["Three trades"]
    assert promoted[0].support == 3


def test_contradictions_block_promotion_at_the_ratio(engine: Engine) -> None:
    # Four supporting against two contradicting fails 3:1. The model is told to propose this
    # anyway and let code refuse it — the refusal is the design, not a failure (F-115).
    record(
        engine,
        "r1",
        LessonSet(candidates=[_candidate("Contested", _ids(4), _ids(2, offset=10))]),
        set(_ids(4)) | set(_ids(2, offset=10)),
        NOW,
    )
    assert active(engine, NOW) == []


def test_reconfirmation_unions_evidence_across_runs(engine: Engine) -> None:
    """Two runs each finding two supporting trades should add up to a promotion.

    Taking only the latest row would leave a real pattern stuck below the bar forever, which is
    the failure that would make the loop unable to learn at all.
    """
    known = set(_ids(5))
    first = LessonSet(candidates=[_candidate("Funding hurt longs", ["t0", "t1", "t2"])])
    record(engine, "r1", first, known, NOW)
    assert active(engine, NOW)[0].support == 3

    later = LessonSet(candidates=[_candidate("Longs were hurt by funding", ["t2", "t3", "t4"])])
    record(engine, "r2", later, known, NOW + timedelta(days=7))
    promoted = active(engine, NOW + timedelta(days=7))
    assert len(promoted) == 1
    # Five distinct trades, not six: t2 appears in both runs and is counted once.
    assert promoted[0].support == 5
    # The newest wording holds the slot: it was written against the most recent evidence.
    assert promoted[0].lesson == "Longs were hurt by funding"


def test_a_lesson_expires_after_the_ttl(engine: Engine) -> None:
    record(engine, "r1", LessonSet(candidates=[_candidate("Stale", _ids(3))]), set(_ids(3)), NOW)
    assert len(active(engine, NOW + timedelta(days=59))) == 1
    assert active(engine, NOW + timedelta(days=61)) == []


def test_refused_candidates_are_kept_with_their_reason(engine: Engine) -> None:
    kept, rejected = record(
        engine,
        "r1",
        LessonSet(candidates=[_candidate("Risk 3% on strong setups", _ids(3))]),
        set(_ids(3)),
        NOW,
    )
    assert kept == 0 and len(rejected) == 1
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT lesson, rejected_for FROM lessons")).all()
    # Kept on purpose: a model that keeps proposing risk rules is drift worth seeing.
    assert stored[0][0] == "Risk 3% on strong setups"
    assert "risk" in stored[0][1].lower()
    assert active(engine, NOW) == []


def test_only_ten_lessons_reach_a_prompt(engine: Engine) -> None:
    candidates = [
        _candidate(f"Distinct pattern number {i}", _ids(3 + i % 3, offset=i * 10)) for i in range(8)
    ]
    known = {tid for c in candidates for tid in c.supporting_trade_ids}
    record(engine, "r1", LessonSet(candidates=candidates[:8]), known, NOW)
    record(engine, "r2", LessonSet(candidates=candidates[:8]), known, NOW)
    assert len(active(engine, NOW)) <= MAX_ACTIVE


def test_an_empty_memory_renders_as_a_dash_not_a_blank() -> None:
    # A blank block in a prompt reads as a bug; `-` reads as "nothing learned yet".
    assert render([]) == "-"
