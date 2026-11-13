"""`argus replay` reads the journal and nothing else (review J-10).

It opened the journal with `open_journal()`, which creates a missing file and applies pending
migrations — so pointing replay at the wrong ARGUS_HOME made an empty journal there, and replaying
an older copy for an investigation silently changed the evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from argus.config import get_settings
from argus.journal.db import open_journal, open_journal_readonly


def test_a_missing_journal_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "journal.db"
    with pytest.raises(FileNotFoundError):
        open_journal_readonly(missing)
    assert not missing.exists()


def test_writes_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "journal.db"
    open_journal(path)
    engine = open_journal_readonly(path)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM runs")).scalar() == 0
    with pytest.raises(OperationalError, match="readonly"), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                " VALUES ('r', 'manager', 'demo', 't', 'OK', '-')"
            )
        )


def test_argus_replay_does_not_create_a_journal() -> None:
    from argus.cli import app

    journal = get_settings().journal_path
    assert not journal.exists()
    result = CliRunner().invoke(app, ["replay", "--last"])
    assert result.exit_code != 0
    assert not journal.exists()
