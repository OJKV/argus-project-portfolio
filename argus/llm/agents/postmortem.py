"""The Post-mortem: what one closed trade taught, judged on process rather than on PnL.

This is the first half of the learning loop. It runs once per closed trade and writes a verdict
the Reflector later reads across thirty of them; whatever bias lives here compounds there. The
bias to fear is hindsight — grading a sound plan as bad because it lost — which is why the schema
carries both `process_verdict` and `would_take_again`. A sound loss must come back `sound` and
`True`, and code can check that agreement without judging the trade itself.

It has no authority at all: nothing it returns changes a position, a size or a threshold. Its
only output is text that shapes future prompts, which is exactly why it is `optional` criticality
— a failed post-mortem costs a lesson, not a trade.
"""

from __future__ import annotations

import json
from typing import Any

from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import PostMortem

AGENT = "postmortem"
PROMPT = "postmortem"


def outcome_bias(report: PostMortem, outcome_r: float) -> str | None:
    """Catch the verdict contradicting itself — which is where hindsight leaks in.

    A first version of this asked whether a losing trade was called flawed without naming a missed
    signal, and it was wrong: under the v2 prompt a trade can be flawed because its *stop* was
    unplayable, with nothing missed at entry at all. It accused two correct answers.

    What is genuinely checkable is agreement between the two judgements. `process_verdict` says
    whether all three tests held; `would_take_again` is the same claim as a commitment. Holding
    one and refusing the other means something went unsaid, and the PnL is usually what it was.
    Worth journalling in production: a model that starts doing this is a model whose lessons
    should stop being promoted.
    """
    if report.process_verdict == "flawed" and report.would_take_again:
        return "would repeat a process it called flawed" + (" — it won" if outcome_r > 0 else "")
    if report.process_verdict == "sound" and not report.would_take_again:
        return "refuses to repeat a process it called sound" + (
            " — it lost" if outcome_r < 0 else ""
        )
    return None


def build_prompt(
    plan: dict[str, Any],
    trail: list[dict[str, Any]] | None,
    outcome: dict[str, Any],
) -> str:
    """Plan, review trail, outcome — and the arithmetic between them already done.

    `compute_trade_flags` exists because v1 of this prompt graded a trade filled 4.3% above its
    planned trigger as well timed: the two prices were both in the payload and the model never
    subtracted them (F-111).
    """
    from argus.features.trade_flags import compute_trade_flags

    return render(
        PROMPT,
        plan=json.dumps(plan, default=str, separators=(",", ":")),
        trail=json.dumps(trail or [], default=str, separators=(",", ":")),
        outcome=json.dumps(outcome, default=str, separators=(",", ":")),
        flags=json.dumps(compute_trade_flags(plan, outcome), default=str, separators=(",", ":")),
    )


def analyse(
    router: Router,
    run_id: str,
    plan: dict[str, Any],
    outcome: dict[str, Any],
    trail: list[dict[str, Any]] | None = None,
    used_families: dict[str, str] | None = None,
) -> CallResult[PostMortem]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(plan, trail, outcome),
        output_type=PostMortem,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )
