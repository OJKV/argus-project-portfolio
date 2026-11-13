"""Prompt templates as versioned artefacts (LLM_AGENTS.md §3, CLAUDE.md).

A prompt is code: changing one changes what the system decides, so each file carries a `version:`
header and a change requires a replay diff. Rendering is pure, which is what lets a test assert
the exact bytes that will reach a provider without calling one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from string import Formatter

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
# README.md documents the directory; it is not a template and carries no version header.
NOT_TEMPLATES = {"README"}
_HEADER = re.compile(r"^version:\s*(\d+)\s*$", re.M)


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    body: str

    @property
    def label(self) -> str:
        return f"{self.name}@v{self.version}"


@cache
def load(name: str) -> Prompt:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"no prompt template {name!r} in {PROMPT_DIR}")
    raw = path.read_text(encoding="utf-8")
    match = _HEADER.search(raw)
    if match is None:
        raise ValueError(f"{path.name} has no `version:` header — prompts are versioned artefacts")
    _, _, body = raw.partition("---\n")
    return Prompt(name=name, version=int(match.group(1)), body=body or raw)


def templates() -> list[str]:
    """Every prompt template in the directory, documentation excluded."""
    return sorted(path.stem for path in PROMPT_DIR.glob("*.md") if path.stem not in NOT_TEMPLATES)


def placeholders(name: str) -> set[str]:
    return {f for _, f, _, _ in Formatter().parse(load(name).body) if f}


def render(name: str, **values: str) -> str:
    """Fill every placeholder, or fail.

    `str.format` would raise on a missing key anyway; the explicit check exists to name *all* of
    them at once. A half-filled prompt reaching a provider wastes the call and is invisible in the
    answer, which is the expensive way to discover a typo.
    """
    missing = placeholders(name) - set(values)
    if missing:
        raise KeyError(f"{name}: no value for {', '.join(sorted(missing))}")
    return load(name).body.format(**values)
