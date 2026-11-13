"""`deploy/checks.sh` is password-free for `operator`, so it must never reach an armed run.

Review 2026-10-07, P-N10: its comment said the shadow arm was safe because "`argus manager`
refuses to run without --shadow, and run_manager raises on shadow=False". Both stopped being true
when Phase 5 armed the manager. The arm is still safe — but only because it passes `--shadow`,
and a comment that names the wrong reason invites someone to drop the flag.
"""

from __future__ import annotations

import re

from .systemd_units import DEPLOY

CHECKS = DEPLOY / "checks.sh"


def _arms() -> dict[str, str]:
    """`name) command ;;` arms of the case statement, comments stripped."""
    body = "\n".join(
        line for line in CHECKS.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    return {m.group(1): m.group(2) for m in re.finditer(r"^\s*([\w-]+)\)\s*(.+?);;", body, re.M)}


def test_every_manager_arm_is_shadow() -> None:
    arms = {k: v for k, v in _arms().items() if " manager" in v}
    assert arms, "no manager arm found — the parser is reading the wrong thing"
    for name, command in arms.items():
        assert "--shadow" in command.split(), f"checks.sh {name} can start an armed manager"


def test_every_review_arm_is_a_dry_run() -> None:
    arms = {k: v for k, v in _arms().items() if " review" in v}
    assert arms
    for name, command in arms.items():
        assert "--dry-run" in command.split(), f"checks.sh {name} can change a position"


def test_no_arm_flattens_halts_or_trades() -> None:
    for name, command in _arms().items():
        for verb in (" flatten", " halt", " resume", "trade.sh"):
            assert verb not in command, f"checks.sh {name} runs{verb}"


def test_the_comment_names_the_real_reason_the_shadow_arm_is_safe() -> None:
    text = " ".join(CHECKS.read_text().split())
    assert "refuses to run without --shadow" not in text
    assert "run_manager raises on shadow=False" not in text
