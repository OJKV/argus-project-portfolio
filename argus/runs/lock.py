"""One order-placing run at a time (ARCHITECTURE.md §12).

Manager, Review and Watchdog can all send orders. Runs are separate short-lived processes
started by systemd timers, so overlap is possible whenever one runs long — and two runs
reconciling the same account at once is how a position gets a second stop, or an entry twice.

The lock is an `flock` on a file, so the kernel releases it when the process dies: a crashed
run can never leave a stale lock behind (which is the failure mode a PID file would have).
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = "run.lock"
POLL_SECONDS = 0.25
# How long a scheduled manager or review queues for the lock before giving its slot up. The
# watchdog ticks at :00/:20/:40 and may hold the lock for a long pass; with no wait, a review at
# :00 lost its slot to the :00 tick, and nothing retried it until the next one (review
# 2026-10-07, P-N6). The units' TimeoutStartSec must cover this plus the run's own budget.
SCHEDULED_RUN_WAIT_S = 300.0
# How long the watchdog queues. The review runs at :00, with the watchdog's own :00 tick; at 120 s
# a review that took the lock first and ran past two minutes cost the watchdog its tick — exit 0,
# no alert, a forty-minute gap in stop repair four times a day (review 2026-10-07, second pass).
# A whole run budget (`run_budget_seconds`) means a review inside its budget is always outwaited;
# the unit's TimeoutStartSec adds this to the watchdog's own pass.
WATCHDOG_WAIT_S = 480.0


# Appended by every `update.sh` — deploy and rollback alike — while it still holds the lock.
DEPLOY_HISTORY = "deploy-history.log"


class LockBusyError(RuntimeError):
    """Another run holds the lock."""


class CodeChangedError(LockBusyError):
    """A deploy finished after this process started, so part of what it imported is old code.

    A run that waits for the lock behind `update.sh` has already imported the CLI, the manager,
    the gate and the adapter from the old checkout, and would import the agents, the pipeline and
    lazily loaded packages from the new one once the deploy lets go — the mixed-code run P-N3
    describes, brought back by the wait P-N6 added (review 2026-10-07, second pass). The caller
    starts over on the new code; anything that only knows `LockBusyError` gives the slot up.
    """


def deployed_since(home: Path, started_at: float) -> bool:
    """Has a deploy recorded itself since `started_at`?"""
    try:
        return (home / DEPLOY_HISTORY).stat().st_mtime >= started_at
    except OSError:
        return False


@contextmanager
def run_lock(
    kind: str, home: Path, timeout_s: float = 0.0, started_at: float | None = None
) -> Iterator[bool]:
    """Hold the run lock, waiting up to `timeout_s`. Raises `LockBusy` if it cannot, and
    `CodeChangedError` if a deploy finished after this process (`started_at`) began."""
    if started_at is None:
        import argus

        started_at = argus.STARTED_AT
    home.mkdir(parents=True, exist_ok=True)
    path = home / LOCK_NAME
    handle = path.open("a+")
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.seek(0)
                    holder = handle.read().strip() or "another run"
                    raise LockBusyError(f"{kind} cannot start: {holder} is running") from None
                time.sleep(POLL_SECONDS)
        if deployed_since(home, started_at):
            raise CodeChangedError(f"{kind}: a deploy finished after this run started")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{kind} pid={os.getpid()} since={time.time():.0f}")
        handle.flush()
        yield True
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
