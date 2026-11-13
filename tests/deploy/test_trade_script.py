"""`deploy/trade.sh` trades the demo account and nothing else (review 2026-10-07, X-N10, P-N1).

It was `exec argus manager --llm "$@"`: the account came from `.env`, the mode was only printed,
and nothing refused `OKX_DEMO=false`. Since `operator` may run it without a password, the same
command
that rehearses on demo would have traded real money the moment `.env` changed. Only the systemd
timers may run against the live account.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from . import sandbox

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

ARMED = {"MODE": "approve", "LIVE_TRADING": "I_UNDERSTAND"}


def _box(tmp_path: Path) -> sandbox.Sandbox:
    box = sandbox.build(tmp_path, scripts=("trade.sh",))
    venv_bin = box.checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    # The real interpreter, so the script reads the account through argus.config.Settings exactly
    # as it would on the box; a recorder in place of the argus CLI, so nothing can trade.
    sandbox._write_exec(venv_bin / "python", f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    sandbox._write_exec(
        venv_bin / "argus", '#!/usr/bin/env bash\necho "argus $*" >> "$SANDBOX_LOG"\nexit 0\n'
    )
    return box


def test_the_live_account_is_refused(tmp_path: Path) -> None:
    box = _box(tmp_path)
    result = box.run("trade.sh", OKX_DEMO="false", LIVE_VENUE="I_UNDERSTAND_REAL_MONEY", **ARMED)
    assert result.returncode != 0
    assert "live" in result.stderr
    assert "argus manager" not in box.calls(), "trade.sh started a run against real money"


def test_a_shadow_run_on_the_live_account_is_refused_too(tmp_path: Path) -> None:
    box = _box(tmp_path)
    result = box.run(
        "trade.sh", "--shadow", OKX_DEMO="false", LIVE_VENUE="I_UNDERSTAND_REAL_MONEY", **ARMED
    )
    assert result.returncode != 0
    assert "argus manager" not in box.calls()


def test_a_config_that_does_not_validate_is_refused(tmp_path: Path) -> None:
    """Unknown is not demo: if the settings cannot be read, nothing starts."""
    box = _box(tmp_path)
    result = box.run("trade.sh", OKX_DEMO="false", **ARMED)
    assert result.returncode != 0
    assert "argus manager" not in box.calls()


def test_the_demo_account_runs_the_real_agents(tmp_path: Path) -> None:
    box = _box(tmp_path)
    result = box.run("trade.sh", OKX_DEMO="true", **ARMED)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "argus manager --llm" in box.calls()


# ── `trade.sh manual …`: one deliberate demo trade through the same grant ─────────────────────


def test_manual_routes_to_the_manual_trade_command(tmp_path: Path) -> None:
    box = _box(tmp_path)
    result = box.run("trade.sh", "manual", "--symbol", "BTC", "--yes", OKX_DEMO="true", **ARMED)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "argus manual-trade --symbol BTC --yes" in box.calls()
    assert "argus manager" not in box.calls()


def test_manual_on_the_live_account_is_refused_like_everything_else(tmp_path: Path) -> None:
    box = _box(tmp_path)
    result = box.run(
        "trade.sh",
        "manual",
        "--symbol",
        "BTC",
        "--yes",
        OKX_DEMO="false",
        LIVE_VENUE="I_UNDERSTAND_REAL_MONEY",
        **ARMED,
    )
    assert result.returncode != 0
    assert "manual-trade" not in box.calls()


def test_the_default_is_still_a_manager_run_and_never_a_manual_trade(tmp_path: Path) -> None:
    box = _box(tmp_path)
    result = box.run("trade.sh", "--shadow", OKX_DEMO="true", **ARMED)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "argus manager --llm --shadow" in box.calls()
    assert "manual-trade" not in box.calls()
