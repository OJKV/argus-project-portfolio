"""The go-live gate is written down, tracked, and complete (review 2026-09-18 P-1, still OPEN).

`BUILD_PLAN.md` describes a Phase-4 go-live gate and both reviews list fix-first items, but there
was no single checklist to run against the demo journal before real money — so "going live" was
one `.env` flip with nothing to tick.
"""

from __future__ import annotations

import subprocess

import pytest

from .systemd_units import REPO

CHECKLIST = REPO / "docs" / "SAFETY.md"

# Review 2026-10-07 §0 "Fix-first (before any live key is armed)".
FIX_FIRST = [
    "R-1", "P-N2", "P-N1", "P-N3", "G-N1", "G-15", "D-1", "G-1", "G-N4", "D-3", "D-10",
    "X-N3", "X-N4", "X-N5", "L-2", "L-5", "J-N1", "P-2", "O-1", "P-N5",
]  # fmt: skip

# BUILD_PLAN.md Phase 4 "Gate (go-live)".
PHASE_4_GATE = [
    "50 shadow decisions",
    "15 closed demo trades",
    "14 days",
    "calibration",
    "fee+funding drag",
    "consensus veto",
    "/why",
]


def _text() -> str:
    return CHECKLIST.read_text()


def test_the_checklist_exists_and_is_not_git_ignored() -> None:
    assert CHECKLIST.exists()
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(CHECKLIST)], cwd=REPO, capture_output=True
    )
    assert ignored.returncode == 1, "docs/* is ignored by default; the checklist must be listed"


@pytest.mark.parametrize("finding", FIX_FIRST)
def test_every_fix_first_item_is_on_it(finding: str) -> None:
    assert f"`{finding}`" in _text(), finding


@pytest.mark.parametrize("item", PHASE_4_GATE)
def test_the_build_plan_phase_4_gate_is_on_it(item: str) -> None:
    assert item in _text(), item


def test_it_names_the_flip_and_the_grilling_gate() -> None:
    text = _text()
    assert "LIVE_VENUE=I_UNDERSTAND_REAL_MONEY" in text
    assert "grilling" in text
    assert "rehearsed" in text
