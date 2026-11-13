"""The Catalyst Extractor: turn headlines into dated, sourced questions for the operator.

Its output never reaches the gate. A candidate goes to Telegram, owner confirms or rejects it, and
only a confirmed one lands in `knowledge/catalysts.yaml` where the dossier can see it. That
human step is the whole design: headlines are the cheapest place in this pipeline to hallucinate,
so nothing extracted from them is trusted until a person has looked.

`filter_sourced()` is the half of the check that does not need a person. The model is told to copy
the `src` of the evidence block it used, and any candidate naming a source that was not in the
prompt is discarded before owner ever sees it — an invented event usually arrives with an invented
citation, and that is checkable arithmetic rather than judgement (CLAUDE.md: code computes what is
checkable, the model judges what is not).
"""

from __future__ import annotations

import re
from datetime import date

from argus.features.dossier import Dossier, evidence_tag
from argus.llm.prompts import load, render
from argus.llm.router import CallResult, Router
from argus.llm.schemas import CatalystCandidate, CatalystList

AGENT = "catalyst"
PROMPT = "catalyst"
MAX_EVIDENCE = 24  # one call for the whole run; beyond this the tail is noise

_SRC = re.compile(r'src="([^"]*)"')


def evidence_block(dossiers: list[Dossier], limit: int = MAX_EVIDENCE) -> str:
    """The run's headlines as one evidence list, newest first, across every symbol."""
    lines: list[str] = []
    for dossier in dossiers:
        for item in dossier.news:
            lines.append(f"[{dossier.symbol}] {evidence_tag(item)}")
    return "\n".join(lines[:limit])


def sources(evidence: str) -> set[str]:
    return set(_SRC.findall(evidence))


def build_prompt(dossiers: list[Dossier], today: date | None = None) -> str:
    return render(
        PROMPT,
        evidence=evidence_block(dossiers) or "(no headlines in the last 48h)",
        today=(today or date.today()).isoformat(),
        symbols=", ".join(d.symbol for d in dossiers) or "-",
    )


def filter_sourced(
    candidates: list[CatalystCandidate], evidence: str
) -> tuple[list[CatalystCandidate], list[CatalystCandidate]]:
    """Split candidates into (cited, invented) by whether their source was in the prompt."""
    known = sources(evidence)
    cited = [c for c in candidates if c.source in known]
    return cited, [c for c in candidates if c.source not in known]


def extract(
    router: Router,
    run_id: str,
    dossiers: list[Dossier],
    today: date | None = None,
    used_families: dict[str, str] | None = None,
) -> CallResult[CatalystList]:
    return router.call(
        run_id=run_id,
        agent=AGENT,
        prompt=build_prompt(dossiers, today=today),
        output_type=CatalystList,
        used_families=used_families,
        prompt_version=load(PROMPT).label,
    )
