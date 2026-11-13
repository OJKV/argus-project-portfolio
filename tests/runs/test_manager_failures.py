"""A manager run that breaks after setup must say so: FAILED in the journal, non-zero to systemd.

Review J-N4 (and J-1/J-2 before it): an exception after setup left the run row RUNNING for ever
and exited through a traceback, while a FAILED run exited 0 — so the unit looked healthy and
`manager_stale` stayed quiet. The run row is the only record of what a run did; it has to close.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

import argus.cli as cli
import argus.runs.manager as manager_mod
from argus.journal.db import open_journal
from argus.runs.manager import ManagerResult, run_manager
from tests.runs.test_manager_shadow import NOW, _deps


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _run_row(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT status, error FROM runs")).mappings().one())


def test_an_exception_after_setup_marks_the_run_failed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise RuntimeError("risk config unreadable")

    monkeypatch.setattr(manager_mod, "step_policies", lambda *a, **k: broken())

    result = run_manager(engine, _deps(), now=NOW, shadow=True)

    row = _run_row(engine)
    assert row["status"] == "FAILED"
    assert "risk config unreadable" in row["error"]
    assert result.status == "FAILED"


def test_a_failed_run_raises_a_critical_alert(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """J-N4: a FAILED manager run alerted nobody; the unit only looked quiet."""
    sent: list[Any] = []
    monkeypatch.setattr(manager_mod.alerts, "dispatch", lambda batch, *a, **k: sent.extend(batch))
    monkeypatch.setattr(
        manager_mod, "step_policies", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )

    run_manager(engine, _deps(), now=NOW, shadow=True)

    failed = [a for a in sent if a is not None and a.key == "manager_failed"]
    assert failed and failed[0].severity == "critical"


def test_an_interrupt_still_closes_the_run_row(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_a: Any, **_k: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(manager_mod, "_record_dossier", interrupted)

    with pytest.raises(KeyboardInterrupt):
        run_manager(engine, _deps(), now=NOW, shadow=True)

    assert _run_row(engine)["status"] == "FAILED"


def test_a_failed_run_exits_non_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    engine = open_journal(tmp_path / "journal.db")
    monkeypatch.setattr("argus.journal.db.open_journal", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_listed_assets", lambda: [])
    monkeypatch.setattr(
        "argus.runs.manager.run_manager",
        lambda *a, **k: ManagerResult(run_id="r" * 32, shadow=True, status="FAILED"),
    )

    result = CliRunner().invoke(cli.app, ["manager", "--shadow", "--equity", "150"])

    assert result.exit_code == 1, result.output
