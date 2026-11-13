"""A fresh box installs the locked dependency set with a known uv (review 2026-09-18 P-12, PARTIAL
on 2026-10-07).

`install.sh` piped whatever `https://astral.sh/uv/install.sh` served that day into a shell, and
ran `uv sync --all-extras` without `--locked` — so the first install on a new box resolved fresh
versions instead of the hash-locked `uv.lock` CI tested, while every later `update.sh` used
`--locked`. A box rebuilt after a VPS loss (RUNBOOK) would run code against dependencies nothing
had ever tested. CI's uv was also "latest", so CI and the box could disagree on lock semantics.
"""

from __future__ import annotations

import re

from .systemd_units import DEPLOY, REPO

INSTALL = DEPLOY / "install.sh"
GATE = REPO / ".github" / "workflows" / "gate.yml"


def _uv_version_in_install() -> str:
    text = INSTALL.read_text()
    assert "https://astral.sh/uv/install.sh" not in text, "unpinned uv installer"
    pinned = re.search(r"^UV_VERSION=(\d+\.\d+\.\d+)$", text, re.M)
    assert pinned, "install.sh must pin UV_VERSION"
    assert "https://astral.sh/uv/$UV_VERSION/install.sh" in text
    return pinned.group(1)


def test_install_pins_the_uv_installer() -> None:
    _uv_version_in_install()


def test_ci_runs_the_same_uv_as_a_fresh_box() -> None:
    text = GATE.read_text()
    step = text[text.index("setup-uv@") :].split("- name:", 1)[0]
    ci = re.search(r'^\s+version:\s*"?([\d.]+)"?\s*$', step, re.M)
    assert ci, "gate.yml must pin setup-uv's version"
    assert ci.group(1) == _uv_version_in_install()


def _sync_lines() -> list[tuple[str, str]]:
    lines = []
    for path in [*sorted(DEPLOY.glob("*.sh")), GATE]:
        for line in path.read_text().splitlines():
            if re.search(r'(\buv|\$UV"?) sync\b', line) and not line.strip().startswith("#"):
                lines.append((path.name, line.strip()))
    return lines


def test_every_sync_on_a_box_or_in_ci_is_locked() -> None:
    lines = _sync_lines()
    assert {name for name, _ in lines} >= {"install.sh", "update.sh", "gate.yml"}
    for name, line in lines:
        assert "--locked" in line, f"{name}: {line}"


def test_install_and_update_install_the_same_extras() -> None:
    """update.sh's gate needs the dev extras on the box (F-082); a fresh install must match."""
    for name, line in _sync_lines():
        if name in {"install.sh", "update.sh"}:
            assert "--all-extras" in line, f"{name}: {line}"
