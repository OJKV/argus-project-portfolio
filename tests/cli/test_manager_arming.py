"""An armed manager run must use the real agents.

F-145: `argus-manager.service` ran `argus manager` without `--llm`, so every scheduled run used
the deterministic stub — which scores every asset +3 against a +4 bar. The first two scheduled
runs proposed 17 plans, all rejected at exactly +3, and the dashboard said "No model calls". The
system looked like a strict gate refusing a mediocre market; it was a placebo.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import argus.cli as cli

UNIT = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "argus-manager.service"


def test_the_scheduled_manager_runs_the_real_agents() -> None:
    exec_line = next(
        line for line in UNIT.read_text().splitlines() if line.startswith("ExecStart=")
    )
    assert "argus manager" in exec_line
    assert "--llm" in exec_line.split()


def test_an_armed_run_refuses_the_deterministic_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub exists for shadow runs and tests. Armed, it is a placebo that looks like a gate."""
    armed = SimpleNamespace(mode="approve", venue="demo")
    monkeypatch.setattr(cli, "get_settings", lambda: armed)
    result = CliRunner().invoke(cli.app, ["manager"])
    assert result.exit_code == 2, result.output
    assert "--llm" in result.output


def test_a_shadow_run_may_still_use_the_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow + stub is the offline smoke path (checks.sh shadow); it must not be caught."""
    armed = SimpleNamespace(mode="approve", venue="demo")
    monkeypatch.setattr(cli, "get_settings", lambda: armed)
    reached: dict[str, bool] = {}

    def stop_here(*_a: object, **_k: object) -> None:
        reached["journal"] = True
        raise SystemExit(0)

    monkeypatch.setattr("argus.journal.db.open_journal", stop_here)
    CliRunner().invoke(cli.app, ["manager", "--shadow"])
    assert reached.get("journal"), "shadow + stub was refused before reaching the run"


def test_an_armed_run_refuses_a_made_up_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review 2026-10-07, second pass: `--equity` is documented "shadow-only" and was honoured in
    armed runs, so the passwordless `sudo -u argus trade.sh --equity 1000000` sized real demo
    orders on an invented balance. Sizing must come from the venue whenever orders can be sent."""
    armed = SimpleNamespace(mode="approve", venue="demo")
    monkeypatch.setattr(cli, "get_settings", lambda: armed)
    reached: dict[str, bool] = {}

    def stop_here(*_a: object, **_k: object) -> None:
        reached["journal"] = True
        raise SystemExit(0)

    monkeypatch.setattr("argus.journal.db.open_journal", stop_here)
    result = CliRunner().invoke(cli.app, ["manager", "--llm", "--equity", "1000000"])
    assert result.exit_code == 2, result.output
    assert "--equity" in result.output and "--shadow" in result.output
    assert not reached, "an armed run with a made-up equity got as far as the journal"


def test_a_shadow_run_may_still_override_equity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The offline shadow path with no exchange access; it must not be caught."""
    armed = SimpleNamespace(mode="approve", venue="demo")
    monkeypatch.setattr(cli, "get_settings", lambda: armed)
    reached: dict[str, bool] = {}

    def stop_here(*_a: object, **_k: object) -> None:
        reached["journal"] = True
        raise SystemExit(0)

    monkeypatch.setattr("argus.journal.db.open_journal", stop_here)
    CliRunner().invoke(cli.app, ["manager", "--shadow", "--equity", "1000"])
    assert reached.get("journal")
