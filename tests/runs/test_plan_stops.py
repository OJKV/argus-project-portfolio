"""Review 2026-09-18 X-6: the production watchdog never received the plan's stop.

`cli.py` built `Watchdog(...)` with no `plan_stops`, so every real repair fell back to 2xATR from
entry — wider than the level the Risk Gate actually approved, and unrelated to the trade's thesis.
The journal has the number; nothing read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.journal.db import open_journal
from argus.runs.manager import run_manager
from argus.runs.watchdog import plan_stops_from_journal
from tests.runs.test_manager_shadow import NOW, _deps


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def test_a_shadow_run_contributes_no_stop(engine: Engine) -> None:
    """Review 2026-10-07 X-N3: a plan nobody placed is an opinion, not a position.

    This test used to assert that a shadow run's approved stop was read back. That was the
    defect: a never-placed plan (here on either side) could replace the stop of the position
    that is actually live. Placed-entry lookups are covered in
    `tests/exchange/test_plan_stops_by_side.py`.
    """
    run_manager(engine, _deps(), now=NOW, shadow=True)
    assert plan_stops_from_journal(engine, [], venue="demo") == {}


def test_an_empty_journal_yields_no_stops(engine: Engine) -> None:
    assert plan_stops_from_journal(engine, [], venue="demo") == {}
