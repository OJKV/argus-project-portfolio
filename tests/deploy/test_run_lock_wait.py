"""Scheduled runs wait for the run lock instead of forfeiting their slot (review 2026-10-07, P-N6).

The manager and the review took the lock with `timeout_s=0`. The watchdog ticks at :00/:20/:40 and
may hold the lock for its whole 600 s budget on a slow venue, so a review at :00 — and a manager
whose watchdog tick ran long — gave up at once, printed a yellow line nobody read, and exited 0.
`Persistent=false` meant nothing retried it: one lost manager slot is twelve hours without a look
at the market.
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from argus.config import Settings
from argus.runs import lock
from argus.runs.lock import LockBusyError, run_lock

from .systemd_units import unit_values

CLI = Path(lock.__file__).resolve().parents[1] / "cli.py"


def _lock_waits_in_cli() -> dict[str, ast.expr]:
    """`timeout_s=` of every `run_lock("<kind>", …)` call in cli.py, keyed by kind."""
    found: dict[str, ast.expr] = {}
    for node in ast.walk(ast.parse(CLI.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_lock"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            timeout = next((k.value for k in node.keywords if k.arg == "timeout_s"), None)
            assert timeout is not None, f"run_lock({node.args[0].value!r}) has no timeout_s"
            found[str(node.args[0].value)] = timeout
    return found


def _seconds(expr: ast.expr) -> float:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, int | float):
        return float(expr.value)
    if isinstance(expr, ast.Name):
        return float(getattr(lock, expr.id))
    raise AssertionError(f"cannot evaluate timeout_s={ast.unparse(expr)}")


@pytest.mark.parametrize("kind", ["manager", "review"])
def test_scheduled_runs_wait_at_least_five_minutes_for_the_lock(kind: str) -> None:
    waits = _lock_waits_in_cli()
    assert kind in waits, f"no run_lock({kind!r}) call in cli.py"
    assert _seconds(waits[kind]) >= 300


def test_a_run_that_waits_gets_the_lock_once_the_holder_finishes(tmp_path: Path) -> None:
    released = threading.Event()

    def hold() -> None:
        with run_lock("watchdog", tmp_path):
            released.wait(5)
            time.sleep(0.3)

    holder = threading.Thread(target=hold)
    holder.start()
    time.sleep(0.1)
    released.set()
    with run_lock("review", tmp_path, timeout_s=5) as held:
        assert held
    holder.join()


def test_a_run_that_does_not_wait_loses_its_slot(tmp_path: Path) -> None:
    """What the old timeout_s=0 did, kept as the contrast."""
    with run_lock("watchdog", tmp_path), pytest.raises(LockBusyError):
        with run_lock("review", tmp_path, timeout_s=0):
            pass


@pytest.mark.parametrize("timer", ["argus-manager.timer", "argus-review.timer"])
def test_a_slot_missed_while_the_box_was_down_runs_at_boot(timer: str) -> None:
    assert unit_values(timer, "Persistent") == ["true"]


@pytest.mark.parametrize("service", ["argus-manager.service", "argus-review.service"])
def test_the_unit_budget_covers_the_lock_wait_plus_the_run(service: str) -> None:
    """Waiting is time systemd counts. A unit that SIGTERMs a run mid-placement because it spent
    five minutes queueing behind the watchdog would trade one lost slot for a worse failure."""
    (budget,) = unit_values(service, "TimeoutStartSec")
    run_budget = Settings().run_budget_seconds
    assert int(budget) >= lock.SCHEDULED_RUN_WAIT_S + run_budget


# ── a run that queued behind a deploy restarts on the new code (review 2026-10-07, 2nd pass) ──
#
# The wait above brings P-N3 back for anything queued behind `update.sh`: before waiting, the
# process has imported cli, the manager, the gate, execution and the adapter from the OLD code;
# after the deploy releases the lock it imports the agents, the pipeline and the rest from the NEW
# checkout and .venv. Every deploy appends to deploy-history.log while it still holds the lock, so
# a history written after the process started means "what you imported is stale".


def _hold_while_deploying(home: Path, deployed: threading.Event) -> threading.Thread:
    def deploy() -> None:
        with run_lock("deploy", home):
            time.sleep(0.3)
            (home / lock.DEPLOY_HISTORY).write_text("A -> B deployed\n")
            deployed.set()

    thread = threading.Thread(target=deploy)
    thread.start()
    time.sleep(0.1)
    return thread


def test_a_run_that_waited_behind_a_deploy_does_not_run_on_mixed_code(tmp_path: Path) -> None:
    started = time.time()
    deployed = threading.Event()
    holder = _hold_while_deploying(tmp_path, deployed)
    ran = False
    with pytest.raises(lock.CodeChangedError):
        with run_lock("manager", tmp_path, timeout_s=5, started_at=started):
            ran = True
    holder.join()
    assert deployed.is_set()
    assert not ran


def test_a_run_started_after_the_last_deploy_proceeds(tmp_path: Path) -> None:
    (tmp_path / lock.DEPLOY_HISTORY).write_text("A -> B deployed\n")
    with run_lock("manager", tmp_path, timeout_s=0, started_at=time.time() + 1) as held:
        assert held


def test_the_code_change_is_a_kind_of_busy_lock() -> None:
    """Anything that only knows LockBusyError still gives the slot up rather than run."""
    assert issubclass(lock.CodeChangedError, LockBusyError)


class _ExecdError(Exception):
    pass


@pytest.mark.parametrize(
    "args",
    [
        ["manager", "--shadow", "--equity", "1000"],
        ["review", "--dry-run"],
        ["watchdog", "--once"],
        ["flatten", "--yes"],
    ],
    ids=lambda a: a[0],
)
def test_a_scheduled_run_restarts_itself_on_the_new_code(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting would lose the slot (nothing retries a manager for twelve hours), so the command
    starts over — the same argv, a fresh interpreter, only the new code."""
    import os
    import sys

    from typer.testing import CliRunner

    import argus.cli as cli
    from argus.runs import lock as lock_module

    def changed(kind: str, home: Path, timeout_s: float = 0.0, **_: object) -> object:
        raise lock_module.CodeChangedError(f"{kind}: a deploy finished while this run waited")

    execs: list[tuple[str, list[str]]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        execs.append((path, list(argv)))
        raise _ExecdError

    monkeypatch.setattr(lock_module, "run_lock", changed)
    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setenv(cli._RESTARTED_ENV, "0")  # recorded, so the helper's "1" is undone
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", lambda *a, **k: object())

    result = CliRunner().invoke(cli.app, args)

    assert isinstance(result.exception, _ExecdError), result.output
    assert execs == [(sys.executable, list(sys.orig_argv))]
    assert os.environ.get(cli._RESTARTED_ENV) == "1"


def test_a_second_deploy_during_the_restarted_wait_gives_the_slot_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from typer.testing import CliRunner

    import argus.cli as cli
    from argus.runs import lock as lock_module

    def changed(kind: str, home: Path, timeout_s: float = 0.0, **_: object) -> object:
        raise lock_module.CodeChangedError("another deploy")

    monkeypatch.setattr(lock_module, "run_lock", changed)
    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("restarted twice"))
    monkeypatch.setenv(cli._RESTARTED_ENV, "1")
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)

    result = CliRunner().invoke(cli.app, ["watchdog", "--once"])
    assert result.exit_code == 0, result.output
    assert "restarted once" in result.output


# ── the watchdog outwaits a review that shares its :00 slot (review 2026-10-07, 2nd pass) ─────


def test_the_watchdog_outwaits_a_run_within_its_budget() -> None:
    """The review runs at :00, with the watchdog's :00 tick. When the review took the lock first
    and ran past 120 s, the watchdog skipped the tick, exit 0, no alert — four times a day, a
    forty-minute gap in stop repair and drawdown checks. It now waits out a run's whole budget."""
    waits = _lock_waits_in_cli()
    assert _seconds(waits["watchdog"]) >= Settings().run_budget_seconds


def test_the_watchdog_unit_budget_covers_its_wait_plus_its_own_pass() -> None:
    """It kept 480 s for its own pass when it waited 120 s inside a 600 s unit; waiting longer
    must not eat into that, or systemd kills a repair halfway."""
    (budget,) = unit_values("argus-watchdog.service", "TimeoutStartSec")
    assert int(budget) >= _seconds(_lock_waits_in_cli()["watchdog"]) + 480
