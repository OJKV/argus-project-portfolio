"""Parallel fetch helper (ARCHITECTURE.md §4: parallel fetches live in `data/`)."""

from __future__ import annotations

import threading
import time

from argus.data.parallel import run_parallel


def test_results_come_back_keyed_by_name_whatever_the_finish_order() -> None:
    tasks = {
        "slow": lambda: (time.sleep(0.05), "slow")[1],
        "fast": lambda: "fast",
    }
    assert run_parallel(tasks, workers=2) == {"slow": "slow", "fast": "fast"}


def test_work_actually_overlaps() -> None:
    started = threading.Barrier(3, timeout=2)

    def wait_for_others() -> str:
        started.wait()  # only returns if three tasks run at once
        return "ok"

    began = time.perf_counter()
    results = run_parallel({str(i): wait_for_others for i in range(3)}, workers=3)
    assert set(results.values()) == {"ok"}
    assert time.perf_counter() - began < 2


def test_one_failing_task_never_takes_down_the_others() -> None:
    def boom() -> str:
        raise TimeoutError("venue down")

    results = run_parallel({"good": lambda: "value", "bad": boom}, workers=2)
    assert results["good"] == "value"
    assert isinstance(results["bad"], TimeoutError)


def test_an_empty_task_set_is_fine() -> None:
    assert run_parallel({}, workers=4) == {}


def test_a_single_task_does_not_need_a_pool() -> None:
    assert run_parallel({"only": lambda: 42}, workers=4) == {"only": 42}
