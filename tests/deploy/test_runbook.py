"""The runbook has a procedure for each incident the review named (2026-09-18 P-15, still OPEN).

It had one line per incident, none rehearsed, and the only working halt — `touch HALT` — was not
in it. These assert the procedures exist and point at commands that exist; whether they work is
what the rehearsal table is for.
"""

from __future__ import annotations

import re

import pytest

from .systemd_units import REPO

RUNBOOK = REPO / "docs" / "SAFETY.md"

PROCEDURES = [
    "Emergency flatten",
    "Manual halt / resume",
    "OKX stop engine outage",
    "VPS lost",
    "Key compromise",
    "Stuck or partially filled order",
    "Orphan algo order / double stop",
    "Ledger mismatch",
    "Disk full",
    "Bad deploy",
]


def _text() -> str:
    return RUNBOOK.read_text()


@pytest.mark.parametrize("title", PROCEDURES)
def test_each_incident_has_its_own_procedure(title: str) -> None:
    assert re.search(rf"^### {re.escape(title)}", _text(), re.M), title


def test_every_procedure_has_a_rehearsal_row() -> None:
    text = _text()
    table = text[text.index("### Rehearsals") :]
    rows = [ln for ln in table.splitlines() if ln.startswith("| ") and "Playbook" not in ln]
    assert len(rows) >= len(PROCEDURES)


def test_the_touch_halt_fallback_is_documented() -> None:
    assert "touch /var/lib/argus/HALT" in _text()


def test_the_runbook_no_longer_points_at_commands_that_do_nothing() -> None:
    """`/halt` and `/close all` are Telegram commands with no poller behind them (P-2)."""
    playbook = _text()[_text().index("## Incident playbook") :]
    assert "→ `/halt`" not in playbook
    assert "`/close all`" not in playbook


def test_the_runbook_documents_the_two_locks_and_the_outcomes_a_human_can_see() -> None:
    text = _text()
    for needle in (
        "deploy.lock",
        "stamp-mismatch",
        "no-green-ref",
        "paged-recently",
        "units-pending",
        "autodeploy.paused",
        "ActiveState",
        "deploy-inflight",
        "recovering-interrupted-deploy",
    ):
        assert needle in text, f"RUNBOOK.md does not mention {needle}"
