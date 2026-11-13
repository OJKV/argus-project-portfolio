"""The nightly backup proves it is a backup (review 2026-09-18 P-6, still OPEN on 2026-10-07).

`backup.sh` copied the journal with `.backup`, gzipped it and exited 0 without ever opening the
copy. `.backup` copies pages faithfully — including corrupt ones — so a damaged journal produced
thirty days of damaged backups, each reported as a success, discovered on the day of the restore.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from .systemd_units import DEPLOY, unit_values

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="needs sqlite3 CLI")


def _backup(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY / "backup.sh")],
        env={**os.environ, "ARGUS_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _journal(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "journal.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=1024")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("CREATE INDEX i ON t(v)")
    conn.executemany(
        "INSERT INTO t VALUES (?)", [(f"row-{n:05d}-{'x' * 40}",) for n in range(2000)]
    )
    conn.commit()
    conn.close()
    return path


def _corrupt(path: Path) -> None:
    """Damage an index page: `.backup` still copies it, `integrity_check` does not pass it."""
    data = bytearray(path.read_bytes())
    page = len(data) // 1024 - 2
    for k in range(page * 1024 + 100, page * 1024 + 400):
        data[k] = 0x55
    path.write_bytes(bytes(data))


def test_a_good_journal_is_backed_up_and_readable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _journal(home)
    result = _backup(home)
    assert result.returncode == 0, result.stdout + result.stderr
    (archive,) = (home / "backup").glob("journal-*.db.gz")
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(archive.read_bytes()))
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (2000,)


def test_a_corrupt_copy_fails_the_backup_loudly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _corrupt(_journal(home))
    result = _backup(home)
    assert result.returncode != 0
    assert "integrity" in result.stdout + result.stderr
    # Nothing that looks like a good backup is left behind for the 30-day rotation to trust.
    assert list((home / "backup").glob("journal-*.db.gz")) == []


def test_a_failed_backup_alerts() -> None:
    assert unit_values("argus-backup.service", "OnFailure") == ["argus-failure@%n.service"]
