"""The Reflector: turn a season of closed trades into a handful of things worth remembering.

The last agent, and the one that closes the loop — Post-mortem judges each trade, this generalises
across them, and `argus/journal/lessons.py` decides what survives into future prompts.

Its authority is the narrowest of any agent and its reach is the longest. It cannot touch a
position, a size or a threshold; all it can do is add a sentence to two prompts. But that sentence
is read on every run for sixty days, so an unfounded one is a bias with a long half-life. The
answer is not to trust the model harder: `record()` requires three distinct supporting trades and
a three-to-one ratio before anything is shown to anyone, checks every cited id against the trades
actually in the prompt, and refuses candidates that try to legislate risk.
"""

from __future__ import annotations

import json
from typing import Any

from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import LessonSet

AGENT = "reflector"
PROMPT = "reflector"
MAX_TRADES = 30  # LLM_AGENTS.md §3.7: beyond this the window stops being one regime


def trade_ids(trades: list[dict[str, Any]]) -> set[str]:
    return {str(t["trade_id"]) for t in trades if t.get("trade_id")}


def build_prompt(trades: list[dict[str, Any]], active: list[str] | None = None) -> str:
    """Closed trades newest-last, and the lessons already active with their support counts.

    The active list is shown so the model can reconfirm or contradict rather than re-propose from
    scratch — a lesson restated in new words with new supporting trades is how evidence
    accumulates, and the fingerprint in `lessons.py` is what makes that work.
    """
    return render(
        PROMPT,
        trades=json.dumps(trades[-MAX_TRADES:], default=str, separators=(",", ":")),
        active="\n".join(active or []) or "(none yet)",
    )


def reflect(
    router: Router,
    run_id: str,
    trades: list[dict[str, Any]],
    active: list[str] | None = None,
    used_families: dict[str, str] | None = None,
) -> CallResult[LessonSet]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(trades, active),
        output_type=LessonSet,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )
