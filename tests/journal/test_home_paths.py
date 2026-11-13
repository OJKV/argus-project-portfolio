"""One ARGUS_HOME, one meaning, whatever directory a command starts in (review J-9).

The journal resolved a relative ARGUS_HOME against the repo root (`db.make_engine`), while the run
lock and the HALT file resolved it against the current directory. A run started from `~` and a
systemd run then shared a journal but used different locks and different HALT files: the lock
that keeps two runs off the same orders, and the file that stops trading, silently split in two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argus.config import REPO_ROOT, Settings


def test_a_relative_home_is_anchored_at_the_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # somewhere that is not the repo
    settings = Settings(mode="demo", argus_home=Path("var/argus"))

    assert settings.argus_home == (REPO_ROOT / "var/argus").resolve()
    assert settings.halt_file.is_absolute()
    assert settings.journal_path.parent == settings.argus_home


def test_an_absolute_home_is_left_alone(tmp_path: Path) -> None:
    assert Settings(mode="demo", argus_home=tmp_path).argus_home == tmp_path
