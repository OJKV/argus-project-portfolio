"""Muse P1-6b: the dashboard is read-only, so starting it must not migrate the journal."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from argus.journal.db import make_engine, open_journal
from argus.journal.db import open_journal_readonly as open_readonly
from argus.ui import app as app_module


def _point_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(app_module, "open_journal_readonly", lambda: open_readonly(path))


def test_startup_does_not_migrate_a_behind_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.db"
    make_engine(path).dispose()
    with make_engine(path).begin() as conn:
        conn.execute(text("CREATE TABLE marker (x INTEGER)"))
    _point_at(monkeypatch, path)
    with pytest.raises(RuntimeError, match="schema"):
        app_module.create_app()
    with make_engine(path).connect() as conn:
        tables = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master")}
    assert "runs" not in tables  # nothing was migrated


def test_startup_serves_a_current_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "journal.db"
    open_journal(path).dispose()
    _point_at(monkeypatch, path)
    assert app_module.create_app() is not None
