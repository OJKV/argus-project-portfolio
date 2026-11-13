"""`argus reflect` writes lessons under a run that exists (review J-N8).

The command passed `new_id()` straight to the reflector, so every `lessons` and `postmortems` row
it wrote pointed at a run_id no `runs` row had — 008/009 carry no foreign key to catch it, and a
lesson could not be traced back to when, in which mode, or by which config it was learned.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from argus.journal.db import open_journal


def test_reflect_journals_its_run_before_writing_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argus.llm.router as router_module
    import argus.runs.reflect as reflect_module
    from argus.cli import app
    from argus.runs.reflect import Reflection

    engine = open_journal()
    seen: dict[str, Any] = {}

    def fake_reflection(engine_: Any, _router: Any, run_id: str, **_: Any) -> Reflection:
        with engine_.connect() as conn:
            seen["row"] = conn.execute(
                text("SELECT kind, status FROM runs WHERE run_id = :r"), {"r": run_id}
            ).one_or_none()
        return Reflection(trades=0)

    monkeypatch.setattr(reflect_module, "run_reflection", fake_reflection)
    monkeypatch.setattr(router_module, "Router", lambda **_: object())
    monkeypatch.setattr("argus.cli._telemetry", lambda *a, **k: None)

    result = CliRunner().invoke(app, ["reflect"])

    assert result.exit_code == 0, result.output
    assert seen["row"] is not None, "reflect wrote under a run_id with no runs row"
    assert tuple(seen["row"]) == ("reflect", "RUNNING")
    with engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM runs WHERE kind = 'reflect'")).scalar()
    assert status == "OK"
