"""Only one order-placing run at a time (ARCHITECTURE.md §12: "lock file prevents overlap")."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from argus.runs.lock import LockBusyError, run_lock


def test_a_single_run_takes_the_lock(tmp_path: Path) -> None:
    with run_lock("manager", tmp_path) as held:
        assert held
        assert (tmp_path / "run.lock").exists()


def test_a_second_run_is_refused_while_the_first_holds_it(tmp_path: Path) -> None:
    with run_lock("manager", tmp_path), pytest.raises(LockBusyError, match="manager"):
        with run_lock("review", tmp_path, timeout_s=0):
            pass


def test_the_lock_is_released_on_the_way_out(tmp_path: Path) -> None:
    with run_lock("manager", tmp_path):
        pass
    with run_lock("review", tmp_path, timeout_s=0):
        pass  # no exception: the first run let go


def test_an_exception_still_releases_the_lock(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), run_lock("manager", tmp_path):
        raise RuntimeError("boom")
    with run_lock("watchdog", tmp_path, timeout_s=0):
        pass


def _hold(path: str, seconds: float) -> None:
    from argus.runs.lock import run_lock as rl

    with rl("manager", Path(path)):
        time.sleep(seconds)


def test_a_crashed_run_never_leaves_a_stale_lock(tmp_path: Path) -> None:
    # The lock is held by the OS on the file handle, so a killed process releases it.
    proc = mp.get_context("spawn").Process(target=_hold, args=(str(tmp_path), 30))
    proc.start()
    time.sleep(1.5)
    with pytest.raises(LockBusyError):
        with run_lock("review", tmp_path, timeout_s=0):
            pass
    proc.terminate()
    proc.join(timeout=5)
    time.sleep(0.3)
    with run_lock("review", tmp_path, timeout_s=0):
        pass  # the dead process's lock is gone


def test_the_holder_is_named_in_the_error(tmp_path: Path) -> None:
    with run_lock("manager", tmp_path):
        try:
            with run_lock("watchdog", tmp_path, timeout_s=0):
                pass
        except LockBusyError as exc:
            assert "manager" in str(exc)
