"""Long-term memory: which lessons the system is allowed to act on, and why.

The learning loop is Post-mortem -> Reflector -> here -> the next run's prompts. This module is
the part that says no. The Reflector proposes; nothing it proposes reaches a prompt until the
evidence clears a bar written in code (LLM_AGENTS.md §3.7): at least three distinct supporting
trades, and at least three times as many supporting as contradicting. A lesson stops being active
sixty days after its last confirmation.

Two guards sit in front of that bar, and both exist because a lesson is the longest-lived thing
any model here can write:

`ground()` drops candidates citing trades that were not in the prompt. A fabricated trade id is
the cheapest possible way to manufacture support for a claim, and it is checkable arithmetic.

`unsafe()` drops candidates that try to legislate risk. The Risk Gate is code, not prompts
(CLAUDE.md), but a lesson is prose injected into the Trader's context, and prose saying "risk 3%
on strong setups" would be a sizing rule arriving through the one door left open. The Trader
cannot set size, so such a lesson cannot change the arithmetic — what it can do is make a model
argue for a worse plan to fit a remembered instruction, which is the same damage a step later.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sqlalchemy import Engine, text

from argus.llm.schemas import LessonCandidate, LessonSet

MIN_SUPPORT = 3
SUPPORT_RATIO = 3  # supporting must outnumber contradicting by this much
TTL_DAYS = 60
MAX_ACTIVE = 10

# Words carrying no claim, removed before fingerprinting so a rephrasing still lands on the same
# lesson. Kept deliberately short: aggressive stripping merges lessons that only look alike.
_NOISE = frozenset(
    "a an the is was are were be been to of in on at for from with and or but that this these"
    " those it its as by than then when while after before over under more less most least do"
    " does did not no".split()
)

# Each pattern is a shape of instruction that must never reach a prompt, whatever evidence stands
# behind it. Matching is deliberately broad — a false positive costs one lesson, a false negative
# puts a risk rule in the Trader's context for sixty days.
_UNSAFE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bleverage\b", re.I), "names leverage, which only the gate sets"),
    (
        re.compile(r"\bposition siz|\bsize (up|down)\b|\b(half|full|double) siz", re.I),
        "tries to set position size",
    ),
    (re.compile(r"\brisk(ing|ed)?\s+(up\s+to\s+)?\d", re.I), "states a risk amount"),
    (
        re.compile(r"\d\s*%\s*(of\s+)?(equity|account|capital|balance)", re.I),
        "states a fraction of equity",
    ),
    (
        re.compile(r"\b(widen|loosen|wider)\w*\s+(the\s+)?stop|more room\b", re.I),
        "asks for a wider stop, which the schema already refuses",
    ),
    (
        re.compile(
            # Up to two words between the verb and the noun, so "ignore the heat limit" is
            # caught as readily as "ignore the limit".
            r"\b(ignore|override|bypass|relax|skip)\w*\s+([\w-]+\s+){0,2}"
            r"(gate|rule|limit|threshold|cooldown|veto)",
            re.I,
        ),
        "asks to override the gate",
    ),
    (
        re.compile(r"\b(add to|double down|average down)\b", re.I),
        "proposes adding to a position, which is not an action this system has",
    ),
)


def fingerprint(lesson: str) -> str:
    """A stable id for *the claim*, so a rephrasing in a later run adds evidence to it.

    Content words as a sorted set, which is crude: "funding high before losses" and "losses before
    funding high" fingerprint alike. That is the direction to err in. A false merge costs two
    similar lessons one prompt slot; a false split leaves a real pattern stuck below the
    three-supporter bar forever, which is the failure that makes the system unable to learn.
    """
    words = sorted({w for w in re.findall(r"[a-z0-9]+", lesson.lower()) if w not in _NOISE})
    return sha256(" ".join(words).encode()).hexdigest()[:16]


def unsafe(lesson: str) -> str | None:
    for pattern, reason in _UNSAFE:
        if pattern.search(lesson):
            return reason
    return None


def ground(
    candidates: list[LessonCandidate], known_trade_ids: set[str]
) -> tuple[list[LessonCandidate], list[tuple[LessonCandidate, str]]]:
    """Split candidates into (usable, rejected-with-reason)."""
    usable: list[LessonCandidate] = []
    rejected: list[tuple[LessonCandidate, str]] = []
    for candidate in candidates:
        cited = set(candidate.supporting_trade_ids) | set(candidate.contradicting_trade_ids)
        if invented := sorted(cited - known_trade_ids):
            rejected.append((candidate, f"cites trades that were not shown: {invented}"))
        elif reason := unsafe(candidate.lesson):
            rejected.append((candidate, reason))
        else:
            usable.append(candidate)
    return usable, rejected


@dataclass(frozen=True)
class ActiveLesson:
    fingerprint: str
    lesson: str
    scope: str
    support: int
    contradiction: int
    last_seen: str

    @property
    def line(self) -> str:
        return f"[{self.scope}] {self.lesson} (supported by {self.support} trades)"


def record(
    engine: Engine,
    run_id: str,
    answer: LessonSet,
    known_trade_ids: set[str],
    now: datetime | None = None,
) -> tuple[int, list[tuple[LessonCandidate, str]]]:
    """Append every candidate, kept or refused, and return (kept, refused-with-reason).

    Refused candidates are stored too, with `rejected_for` set. A model that keeps proposing
    sizing rules is worth knowing about, and deleting the evidence of it would hide a drift that
    matters more than any single lesson.
    """
    from argus.journal.db import new_id

    stamp = (now or datetime.now(UTC)).isoformat()
    usable, rejected = ground(list(answer.candidates), known_trade_ids)
    rows = [
        {
            "lesson_id": new_id(),
            "run_id": run_id,
            "created_at": stamp,
            "fingerprint": fingerprint(candidate.lesson),
            "lesson": candidate.lesson,
            "scope": candidate.scope,
            "supporting": json.dumps(sorted(set(candidate.supporting_trade_ids))),
            "contradicting": json.dumps(sorted(set(candidate.contradicting_trade_ids))),
            "confidence_pct": candidate.confidence_pct,
            "rejected_for": reason,
        }
        for candidate, reason in [(c, None) for c in usable] + rejected  # type: ignore[list-item]
    ]
    if rows:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO lessons (lesson_id, run_id, created_at, fingerprint, lesson,"
                    " scope, supporting, contradicting, confidence_pct, rejected_for)"
                    " VALUES (:lesson_id, :run_id, :created_at, :fingerprint, :lesson, :scope,"
                    " :supporting, :contradicting, :confidence_pct, :rejected_for)"
                ),
                rows,
            )
    return len(usable), rejected


def active(
    engine: Engine, now: datetime | None = None, limit: int = MAX_ACTIVE
) -> list[ActiveLesson]:
    """Which lessons have earned a place in the next run's prompts.

    Evidence is unioned across every row sharing a fingerprint inside the window, so reconfirming
    a lesson with two fresh trades genuinely strengthens it rather than restating it. Refused rows
    are excluded here but kept in the table.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=TTL_DAYS)).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT fingerprint, lesson, scope, supporting, contradicting, created_at"
                " FROM lessons WHERE rejected_for IS NULL AND created_at >= :cutoff"
                " ORDER BY created_at"
            ),
            {"cutoff": cutoff},
        ).all()

    merged: dict[str, dict[str, Any]] = {}
    for fp, lesson, scope, supporting, contradicting, created_at in rows:
        bucket = merged.setdefault(
            fp, {"lesson": lesson, "scope": scope, "sup": set(), "con": set(), "last": created_at}
        )
        bucket["sup"].update(json.loads(supporting))
        bucket["con"].update(json.loads(contradicting))
        # The newest wording wins the slot: it was written against the most recent evidence.
        bucket["lesson"], bucket["scope"], bucket["last"] = lesson, scope, created_at

    promoted = [
        ActiveLesson(
            fingerprint=fp,
            lesson=bucket["lesson"],
            scope=bucket["scope"],
            support=len(bucket["sup"]),
            contradiction=len(bucket["con"]),
            last_seen=bucket["last"],
        )
        for fp, bucket in merged.items()
        if len(bucket["sup"]) >= MIN_SUPPORT
        and len(bucket["sup"]) >= SUPPORT_RATIO * len(bucket["con"])
    ]
    promoted.sort(key=lambda lesson: (-lesson.support, lesson.lesson))
    return promoted[:limit]


def render(lessons: list[ActiveLesson]) -> str:
    """The `{lessons}` block. `-` rather than an empty string: a blank looks like a bug."""
    return "\n".join(f"- {lesson.line}" for lesson in lessons) or "-"
