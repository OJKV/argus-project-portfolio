"""What GitHub's gate checks, compared with what the repo ships (review 2026-10-07, P-N8, P-N11).

CI ran `bash -n` over update.sh, checks.sh, install.sh and backup.sh — and not over
install-units.sh, trade.sh or grant-claude.sh, the three that touch root or orders. A syntax error
in any of them is found on the box, by the person it locks out.
"""

from __future__ import annotations

import re

from .systemd_units import DEPLOY, REPO

GATE = REPO / ".github" / "workflows" / "gate.yml"


def _step_run(name: str) -> str:
    text = GATE.read_text()
    match = re.search(rf"- name: {re.escape(name)}\n\s+run: (.+)", text)
    assert match, f"no '{name}' step in gate.yml"
    return match.group(1)


def test_ci_syntax_checks_every_deploy_script() -> None:
    checked = set(re.findall(r"deploy/[\w.-]+\.sh", _step_run("shell syntax")))
    shipped = {f"deploy/{p.name}" for p in DEPLOY.glob("*.sh")}
    assert shipped <= checked, f"not syntax-checked in CI: {sorted(shipped - checked)}"


def test_make_type_checks_what_ci_type_checks() -> None:
    """P-N11: `make type` ran a bare `uv run mypy` — a different scope from the gate, so a local
    `make gate1` could pass or fail on files CI never looks at."""
    ci = _step_run("mypy --strict on the money paths").strip()
    make = re.search(r"^type:\s*;\s*(.+)$", (REPO / "Makefile").read_text(), re.M)
    assert make, "no `type:` target in the Makefile"
    assert make.group(1).strip() == ci
