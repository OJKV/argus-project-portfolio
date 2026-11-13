"""Does a plan's evidence name anything the model was actually shown? (L-10, non-negotiable 6.)

The schema can refuse a non-zero score with no citation, but it cannot tell a real citation from
an invented one — only something holding the dossier can. Until 2026-10-07 that check lived only
in the golden scorer, so it measured models offline while a live plan could justify a score by
citing a field that does not exist. The matching rules are the scorer's, moved here unchanged so
the live check and the measurement cannot drift apart: `golden.py` imports them from this module.
"""

from __future__ import annotations

import json
import re
from typing import Any

from argus.features.dossier import Dossier, render
from argus.llm.schemas import TradePlan

# A headline renders as `<evidence src=.. asof=.. class=..>title</evidence>`, so its parts are
# what a model can name inside one: `news[0].title` is the headline itself, not an invention.
_EVIDENCE_PARTS = ("title", "src", "asof", "class")
# Where the path a model cites ends and its commentary begins: `tech.px: 106200`,
# `tech.px (106200)`, `tech.ema4h.order = bull`, `news[0] Spot ETF inflows...`.
_PATH_END = re.compile(r"[=:(\s]")
_INDEX = re.compile(r"\[\d+\]")


def evidence_paths(dossier: Dossier) -> set[str]:
    """Every key a model could legitimately cite, harvested from the rendered dossier.

    Taken from the rendering rather than the object: the model only ever sees the rendering, so
    that is the only vocabulary it can honestly use.
    """
    return rendered_paths(render(dossier))


def rendered_paths(rendered: str) -> set[str]:
    """The same vocabulary from a rendering already in hand — `dossiers.payload` in the journal.

    Lets a journaled plan be re-checked against exactly what its model was shown, with no
    network and no rebuilt dossier.
    """
    paths: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{prefix}.{key}" if prefix else str(key)
                paths.add(here)
                paths.add(str(key))
                walk(value, here)
        elif isinstance(node, str):
            if node.startswith("<evidence"):
                for part in _EVIDENCE_PARTS:
                    paths.add(part)
                    paths.add(f"{prefix}.{part}" if prefix else part)
            # Identifier-like VALUES are part of the vocabulary too. The cross-market block
            # stores {"src": "fred", "metric": "policy_rate_pct"}, so `fred.policy_rate_pct`
            # names real data; counting only keys made that look like fabrication.
            token = node.strip()
            if token and " " not in token and len(token) <= 40:
                paths.add(token)
            # A gap renders as the sentence "onchain: missing", and a model citing `gaps.onchain`
            # is pointing at a fact the prompt really contains. Refusing to trade because data is
            # absent is behaviour worth rewarding, so the name before the colon is citable
            # (F-116 — the sixth time this scorer accused a model of seeing what it was shown).
            name, _, rest = token.partition(":")
            if rest and name and " " not in name and len(name) <= 40:
                paths.add(name)
                paths.add(f"{prefix}.{name}" if prefix else name)
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix)

    walk(json.loads(rendered), "")
    return paths


def leaf(citation: str) -> str:
    """The deepest named segment of a citation, ignoring the value after it and list indices.

    `news.0` and `news[0]` mean "the first headline" — precision, not fabrication — so an index
    is dropped rather than treated as a name. Taking it literally once scored the most accurate
    model as inventing evidence four times out of ten, which nearly got it dropped. Likewise the
    path ends at the first `=`, `:`, `(` or space: `tech.ema4h.order: bull` cites `order`, and
    reading `order: bull` as the name refused a real citation before consensus ever saw the plan.
    """
    path = _PATH_END.split(_INDEX.sub("", citation).strip(), maxsplit=1)[0]
    segments = [s for s in path.split(".") if s and not s.isdigit()]
    return segments[-1] if segments else ""


def unsupported_scores(plan: TradePlan, dossier: Dossier) -> dict[str, list[str]]:
    """Non-zero scores none of whose citations name anything in the dossier, with what they cited.

    A citation of a field that does not exist is an uncited score wearing a path, and the schema
    already refuses uncited scores (`TradePlan._check`), so this gets the same answer — in code,
    before any agent or the gate weighs the plan. Deliberately not stricter than that: a factor
    with one real citation and one invented one passes here. The scorer's leaf matching has
    accused honest models of fabrication six times (F-116), and a false positive in the live
    chain costs a trade, so only the unambiguous case is refused; the Red Team reads the rest.
    """
    known = evidence_paths(dossier)
    unsupported: dict[str, list[str]] = {}
    for name in plan.scores.non_zero():
        cited = plan.evidence.get(name, [])
        if not any(leaf(path) in known for path in cited):
            unsupported[name] = list(cited)
    return unsupported
