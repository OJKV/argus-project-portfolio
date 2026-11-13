"""Run independent fetches at the same time (ARCHITECTURE.md §4).

A Manager run is ~26 sequential HTTP calls per asset, which put a cold 15-asset run at the whole
8-minute budget (FAILURE_LOG F-059). The calls are independent, so they overlap.

Threads rather than `asyncio.gather`: every fetcher is synchronous `httpx`, and the two pieces of
shared state they touch — the per-host token bucket and the per-host circuit breaker — are lock
protected. The rate limits are still enforced, so overlapping work cannot become rude.

An exception is *returned*, never raised: one dead source degrades its family, it does not abort
the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

MAX_WORKERS = 8


def run_parallel[T](
    tasks: Mapping[str, Callable[[], T]], workers: int = 4
) -> dict[str, T | BaseException]:
    """Run each task and return `{name: result_or_exception}`."""
    if not tasks:
        return {}
    if len(tasks) == 1:
        name, task = next(iter(tasks.items()))
        return {name: _call(task)}
    limit = max(1, min(workers, MAX_WORKERS, len(tasks)))
    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="argus-fetch") as pool:
        futures = {name: pool.submit(_call, task) for name, task in tasks.items()}
        return {name: future.result() for name, future in futures.items()}


def _call[T](task: Callable[[], T]) -> T | BaseException:
    try:
        return task()
    except Exception as exc:  # noqa: BLE001 - returned to the caller, which decides
        return exc


def value_or_none(result: Any) -> Any:
    return None if isinstance(result, BaseException) else result
