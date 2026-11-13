"""The weekly reflection: read the closed trades, propose lessons, let code decide what survives.

The only run that changes how future runs think. It places no orders, reads no market data and
touches no position — it reads the journal, asks one model one question, and appends to `lessons`.

Everything that makes it safe is in `argus/journal/lessons.py` rather than here: three distinct
supporting trades, a three-to-one ratio against contradictions, every cited trade checked against
the ones actually shown, and any candidate that tries to legislate risk discarded and recorded.
This module's job is to run that gauntlet and report honestly what got through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine

from argus.journal import lessons as memory
from argus.journal.trades import MAX_WINDOW, closed_trades
from argus.llm.agents.reflector import reflect as ask_reflector
from argus.llm.agents.reflector import trade_ids
from argus.llm.router import AllModelsFailedError, Router


@dataclass
class Reflection:
    trades: int
    proposed: int = 0
    kept: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)
    active: list[memory.ActiveLesson] = field(default_factory=list)
    model: str = "-"
    note: str = ""
    failed: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed


def run_reflection(
    engine: Engine,
    router: Router,
    run_id: str,
    now: datetime | None = None,
    window: int = MAX_WINDOW,
    *,
    venue: str,
) -> Reflection:
    """Reflect over the closed trades of one account: one journal holds demo and live (F-152)."""
    trades = closed_trades(engine, limit=window, venue=venue)
    if len(trades) < memory.MIN_SUPPORT:
        # Fewer trades than a lesson needs supporters. Asking anyway would spend a call to be told
        # nothing, and would tempt the model to generalise from a sample it cannot generalise from.
        return Reflection(
            trades=len(trades),
            active=memory.active(engine, now),
            note=f"{len(trades)} closed trades; a lesson needs {memory.MIN_SUPPORT} supporters",
        )

    known = trade_ids(trades)
    active_before = memory.active(engine, now)
    try:
        result = ask_reflector(router, run_id, trades, [lesson.line for lesson in active_before])
    except AllModelsFailedError as exc:
        # Optional criticality: a missed reflection costs a week of learning, not a trade. The
        # previously active lessons stay active — they were promoted on their own evidence.
        return Reflection(
            trades=len(trades), active=active_before, failed=str(exc), note="reflection skipped"
        )

    answer = result.output
    kept, refused = memory.record(engine, run_id, answer, known, now)
    return Reflection(
        trades=len(trades),
        proposed=len(answer.candidates),
        kept=kept,
        refused=[(candidate.lesson, reason) for candidate, reason in refused],
        active=memory.active(engine, now),
        model=result.ref.label,
        note=answer.note,
    )
