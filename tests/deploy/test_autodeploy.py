"""`deploy/autodeploy.sh` run for real, against a scratch repo and journal (see sandbox.py).

The timer runs it every five minutes. The design is "test outside the lock, swap inside it": a
commit supplied through `deploy/green` is checked out in a staging worktree and put through
the whole
suite there, with no run lock held, and only then does `update.sh --pretested` take the lock for the
few seconds the swap needs. What these pin down is everything that must NOT happen: a tick that
re-tests a commit that already failed, a swap under an eval or a trade, a branch moved backwards,
the production journal reaching the staged tests, and a human being woken more than once for the
same commit.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from argus.runs.lock import run_lock

from . import sandbox
from .systemd_units import DEPLOY

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# A Type=oneshot unit that is running is "activating" — never "active" — so `systemctl is-active`
# says no for exactly the jobs autodeploy must not deploy under (review H2). States come from
# $FAKE_UNIT_STATES ("unit=state ..."), or $FAKE_ACTIVE_UNITS (running oneshots: "activating"), or
# a file a test (or a fake uv) writes mid-run.
SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$SANDBOX_LOG"
state_of() {
  local unit=$1 pair active
  for pair in ${FAKE_UNIT_STATES:-}; do
    if [ "${pair%%=*}" = "$unit" ]; then echo "${pair#*=}"; return; fi
  done
  for active in ${FAKE_ACTIVE_UNITS:-} $(cat "$SANDBOX_LOG.active" 2>/dev/null); do
    if [ "$active" = "$unit" ]; then echo activating; return; fi
  done
  echo inactive
}
case "$1" in
  is-active) for unit; do :; done; [ "$(state_of "$unit")" = active ] && exit 0; exit 3 ;;
  show) for unit; do :; done; state_of "$unit"; exit 0 ;;
esac
exit 1
"""

# The password-free installer rule, and the UI restart. Everything else sudo is asked for succeeds.
SUDO = """#!/usr/bin/env bash
echo "sudo $*" >> "$SANDBOX_LOG"
case "$*" in
  *argus-install-units*) exit "${FAKE_INSTALLER_EXIT:-0}" ;;
esac
exit 0
"""

# `nice -n 15 ionice -c3 CMD…`: recorded, then CMD runs. (ionice does not exist on macOS.)
NICE = """#!/usr/bin/env bash
echo "nice $*" >> "$SANDBOX_LOG"
shift 2
exec "$@"
"""
IONICE = """#!/usr/bin/env bash
echo "ionice $*" >> "$SANDBOX_LOG"
shift
exec "$@"
"""

# `timeout SECONDS CMD…`, recorded apart from the uv/sudo log (stock macOS has no timeout). The
# pytest step can be made to time out.
TIMEOUT = """#!/usr/bin/env bash
echo "timeout $*" >> "$SANDBOX_LOG.timeout"
if [ "$1" = -k ]; then shift 2; fi
shift
case "$*" in
  *ls-remote*) [ -z "${FAKE_LSREMOTE_NOISE:-}" ] || echo "Warning: Permanently added a host" >&2 ;;
esac
if [ -n "${FAKE_TIMEOUT_PYTEST:-}" ]; then
  case "$*" in *pytest*) exit 124 ;; esac
fi
exec "$@"
"""

# sha256 of stdin. With FAKE_BAD_HASH it lies, but only to update.sh (autodeploy starts it with
# ARGUS_DEPLOY_LOCK_WAIT_S set), so the stamp autodeploy wrote is not what update.sh computes.
SHA256 = f"""#!{sys.executable}
import hashlib, os, sys
digest = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()
if os.environ.get("FAKE_BAD_HASH") and os.environ.get("ARGUS_DEPLOY_LOCK_WAIT_S"):
    digest = "0" * 64
print(digest + "  -")
"""

LOCK_V1 = b"lock v1\n"


def _box(tmp_path: Path, v2_touches: str = "") -> sandbox.Sandbox:
    box = sandbox.build(tmp_path, scripts=("update.sh", "autodeploy.sh"), v2_touches=v2_touches)
    sandbox._write_exec(box.bin / "systemctl", SYSTEMCTL)
    sandbox._write_exec(box.bin / "sudo", SUDO)
    sandbox._write_exec(box.bin / "nice", NICE)
    sandbox._write_exec(box.bin / "ionice", IONICE)
    sandbox._write_exec(box.bin / "timeout", TIMEOUT)
    sandbox._write_exec(box.bin / "sha256sum", SHA256)
    return box


def _tick(box: sandbox.Sandbox, **env: str) -> subprocess.CompletedProcess[str]:
    """One timer tick. The swap's lock wait is shortened so a busy lock does not cost a minute."""
    return box.run(
        "autodeploy.sh",
        ARGUS_ENABLE_PORTFOLIO_AUTODEPLOY="I_UNDERSTAND",
        ARGUS_AUTODEPLOY_LOCK_WAIT_S="1",
        **env,
    )


def test_portfolio_autodeploy_is_disabled_without_explicit_opt_in(tmp_path: Path) -> None:
    box = _box(tmp_path)
    before = box.calls()
    result = box.run("autodeploy.sh", ARGUS_ENABLE_PORTFOLIO_AUTODEPLOY="")
    assert result.returncode == 0
    assert "portfolio auto-deploy disabled" in result.stdout
    assert box.calls() == before


def _at_v2(box: sandbox.Sandbox) -> str:
    """Production already runs v2 — what a manual `update.sh` leaves behind."""
    v1 = box.git("rev-parse", "HEAD")
    box.git("fetch", "-q", "origin")
    box.git("merge", "-q", "--ff-only", "origin/main")
    v2 = box.git("rev-parse", "HEAD")
    (box.home / "deploy-history.log").write_text(f"2026-10-11T00:00:00Z {v1} -> {v2} deployed\n")
    return v2


def _timeouts(box: sandbox.Sandbox) -> str:
    path = box.root / "calls.log.timeout"
    return path.read_text() if path.exists() else ""


def _events(box: sandbox.Sandbox) -> list[dict[str, str]]:
    path = box.home / "autodeploy.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _results(box: sandbox.Sandbox) -> list[str]:
    return [event["result"] for event in _events(box)]


def _uv(box: sandbox.Sandbox) -> list[str]:
    """Each `uv …` the fake saw, one entry per call (the nice/ionice wrapper lines are not)."""
    return [line for line in box.calls().splitlines() if line.startswith("uv ")]


def _count(box: sandbox.Sandbox, call: str) -> int:
    return _uv(box).count(call)


@contextmanager
def _autodeploy_running(home: Path) -> Iterator[None]:
    with (home / "autodeploy.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


# ── a tick with nothing to do ────────────────────────────────────────────────────────────────


def test_a_tick_with_nothing_new_exits_at_once_without_uv_or_the_run_lock(tmp_path: Path) -> None:
    """Every five minutes, forever: the common case must cost one `git fetch`. It must not sync,
    test, or touch the run lock — the old update.sh did all three on a no-change run."""
    box = _box(tmp_path)
    v2 = _at_v2(box)
    box.promote(v2)

    # A trade is running; a tick that asked for the lock would block. (started_at in the future:
    # the history file _at_v2 wrote is newer than this process, which would read as "code changed".)
    with run_lock("manager", box.home, started_at=time.time() + 60):
        started = time.monotonic()
        result = _tick(box)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < 10
    assert box.calls() == "", "no uv, sudo or systemctl on a no-change tick"
    assert not (box.home / "staging").exists()
    assert _results(box) == ["up-to-date"]


def test_a_second_idle_tick_does_not_grow_the_log(tmp_path: Path) -> None:
    box = _box(tmp_path)
    box.promote(_at_v2(box))

    _tick(box)
    _tick(box)

    assert _results(box) == ["up-to-date"]


def test_before_the_first_promotion_nobody_is_paged_however_long_it_takes(tmp_path: Path) -> None:
    """A missing `deploy/green` is not a network failure: the ref may not yet exist
    after the merge, which can take longer than the fetch-failure alert (12 ticks) tolerates."""
    box = _box(tmp_path)  # origin has main and no deploy/green yet

    codes = [_tick(box).returncode for _ in range(15)]

    assert codes == [0] * 15
    assert box.calls() == ""
    assert _results(box) == ["no-green-ref"], "logged once, not 15 times"
    assert not (box.home / "autodeploy.fetch-failures").exists()

    v2 = box.promote()
    assert _tick(box).returncode == 0
    assert box.git("rev-parse", "HEAD") == v2


def test_an_idle_tick_asks_the_remote_one_question_and_every_git_call_is_bounded(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    box.promote(_at_v2(box))

    assert _tick(box).returncode == 0

    seen = _timeouts(box)
    assert "timeout -k 30 60 git ls-remote --exit-code origin refs/heads/deploy/green" in seen
    assert "git fetch" not in seen, "nothing changed, so there is nothing to fetch"


def test_a_fetch_is_bounded_too(tmp_path: Path) -> None:
    box = _box(tmp_path)
    box.promote()

    assert _tick(box).returncode == 0

    assert "timeout -k 30 120 git fetch" in _timeouts(box)


def test_a_green_ref_behind_production_is_ignored_quietly(tmp_path: Path) -> None:
    """A manual `update.sh` can run ahead of the configured deployment ref. That resolves
    itself; it is not a non-fast-forward worth an alert."""
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    _at_v2(box)
    box.promote(v1)

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.calls() == ""
    assert _results(box) == ["ahead-of-green"]


def test_the_pause_file_stops_the_tick_before_it_touches_anything(tmp_path: Path) -> None:
    """`systemctl stop argus-autodeploy.timer` needs root; this is the pause argus can set."""
    box = _box(tmp_path)
    box.promote()
    (box.home / "autodeploy.paused").write_text("")
    box.git("remote", "set-url", "origin", str(tmp_path / "nowhere"))  # not even a fetch

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.calls() == ""
    assert _results(box) == ["paused"]
    assert not (box.home / "autodeploy.fetch-failures").exists()


def test_two_ticks_never_overlap(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    box.promote()

    with _autodeploy_running(box.home):
        result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.calls() == ""
    assert box.git("rev-parse", "HEAD") == v1


# ── the happy path ───────────────────────────────────────────────────────────────────────────


def test_a_green_commit_is_staged_tested_swapped_and_recorded(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert (box.checkout / "app.txt").read_text() == "v2\n"

    uv = _uv(box)
    mypy = "uv run mypy argus/gate argus/exchange argus/execution argus/journal/states.py"
    smoke = (
        "uv run python -c "
        "import argus.cli, argus.runs.manager, argus.runs.review, argus.runs.watchdog"
    )
    order = [
        uv.index("uv sync --locked --all-extras"),
        uv.index("uv run ruff check ."),
        uv.index(mypy),
        uv.index("uv run pytest -q"),
        uv.index("uv sync --locked --all-extras --quiet"),
        uv.index(smoke),
        uv.index("uv run argus --help"),
        uv.index("uv run python -c from argus.journal.db import open_journal; open_journal()"),
    ]
    assert order == sorted(order), uv
    assert _count(box, "uv run pytest -q") == 1, "the swap must not test a second time"

    stamp = (box.home / "staged-ok").read_text().split()
    assert stamp == [v2, hashlib.sha256(LOCK_V1).hexdigest()]

    (history,) = (box.home / "deploy-history.log").read_text().splitlines()
    assert f"{v1} -> {v2} deployed" in history
    (event,) = [e for e in _events(box) if e["result"] == "deployed"]
    assert event["target"] == v2 and event["ts"].endswith("Z")


def test_the_suite_runs_in_the_staging_checkout_with_a_throwaway_journal(tmp_path: Path) -> None:
    """The tests must never see the production journal, and must not run in the live checkout (whose
    venv the running timers are using)."""
    box = _box(tmp_path)
    box.promote()

    _tick(box)

    (pytest_ctx,) = re.findall(r"ctx:run:pytest pwd=(\S+) ARGUS_HOME=(\S+)", box.calls())
    cwd, argus_home = map(Path, pytest_ctx)
    assert cwd.resolve() == (box.home / "staging").resolve()
    assert argus_home.resolve() != box.home.resolve()
    assert not argus_home.exists(), "the throwaway directory is removed afterwards"
    assert box.journal_rows() == ["before the deploy"]


def test_staging_runs_at_the_lowest_priority(tmp_path: Path) -> None:
    """The staging process must be bounded under resource contention."""
    box = _box(tmp_path)
    box.promote()

    _tick(box)

    calls = box.calls()
    assert "nice -n 19 ionice -c3" in calls
    assert calls.index("nice -n 19") < calls.index("\nuv run pytest -q")
    lines = calls.splitlines()
    last_nice = max(i for i, ln in enumerate(lines) if ln.startswith("nice "))
    assert last_nice < lines.index("uv sync --locked --all-extras --quiet"), (
        "the swap, which holds the locks, must not be deprioritised"
    )


def test_the_staging_checkout_is_reused_and_cleaned_but_keeps_its_venv(tmp_path: Path) -> None:
    box = _box(tmp_path)
    box.promote()
    assert _tick(box).returncode == 0
    staging = box.home / "staging"
    (staging / ".venv").mkdir()
    (staging / ".venv" / "keep").write_text("a 1 GB venv")
    (staging / "left-by-the-last-run.txt").write_text("junk")
    v3 = box.push_main({"app.txt": "v3\n"}, "v3")
    box.promote(v3)

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v3
    assert not (staging / "left-by-the-last-run.txt").exists()
    assert (staging / ".venv" / "keep").exists()
    assert box.git("rev-parse", "HEAD", cwd=staging) == v3


def test_a_broken_staging_checkout_is_rebuilt_instead_of_failing_every_commit(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    box.promote()
    assert _tick(box).returncode == 0
    (box.home / "staging" / ".git").write_text("gitdir: /nonexistent/worktrees/staging\n")
    v3 = box.push_main({"app.txt": "v3\n"}, "v3")
    box.promote(v3)

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v3
    assert box.git("rev-parse", "HEAD", cwd=box.home / "staging") == v3


def test_every_outcome_is_one_json_line_with_a_time_a_target_and_a_result(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v2 = box.promote()
    _tick(box)

    for event in _events(box):
        assert set(event) >= {"ts", "target", "result"}
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", event["ts"])
    assert [e["target"] for e in _events(box)][-1] == v2


# ── staging failure ──────────────────────────────────────────────────────────────────────────


def test_a_failing_suite_marks_the_commit_failed_and_production_is_untouched(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    result = _tick(box, FAKE_PYTEST_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert (box.home / "deploy-failed").read_text().strip() == v2
    assert not (box.home / "staged-ok").exists()
    assert "restart argus-ui" not in box.calls() and "open_journal" not in box.calls()
    assert _results(box)[-1] == "staging-failed"


def test_a_failed_staging_does_not_touch_the_history_the_runs_watch(tmp_path: Path) -> None:
    """`deploy-history.log`'s mtime is the signal a queued manager or review uses to decide the
    code changed under it and restart (argus.runs.lock.deployed_since). A rejected commit changes
    no code, and this append happens outside the run lock — so it must not be written there."""
    box = _box(tmp_path)
    box.promote()
    history = box.home / "deploy-history.log"
    history.write_text("earlier deploy\n")
    before = history.stat().st_mtime_ns

    assert _tick(box, FAKE_PYTEST_EXIT="1").returncode == 1

    assert history.read_text() == "earlier deploy\n"
    assert history.stat().st_mtime_ns == before


def test_the_next_tick_for_the_same_failed_commit_does_not_test_again_or_page_again(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    box.promote()
    assert _tick(box, FAKE_PYTEST_EXIT="1").returncode == 1
    calls_after_failure = box.calls()

    result = _tick(box)  # the suite would pass now; it must not be asked

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.calls() == calls_after_failure
    assert _results(box)[-1] == "skipped-failed"


def test_a_newer_commit_is_tried_even_after_an_older_one_failed(tmp_path: Path) -> None:
    box = _box(tmp_path)
    box.promote()
    assert _tick(box, FAKE_PYTEST_EXIT="1").returncode == 1
    v3 = box.push_main({"app.txt": "v3\n"}, "the fix")
    box.promote(v3)

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v3


@pytest.mark.parametrize("step", ["FAKE_RUFF_EXIT", "FAKE_SYNC_EXIT"])
def test_any_failing_staging_step_fails_the_commit(tmp_path: Path, step: str) -> None:
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    result = _tick(box, **{step: "1"})

    assert result.returncode == 1, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert (box.home / "deploy-failed").read_text().strip() == v2


# ── the swap ─────────────────────────────────────────────────────────────────────────────────


def test_a_busy_run_lock_defers_the_swap_and_the_next_tick_does_not_retest(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    with run_lock("manager", box.home):
        busy = _tick(box)

    assert busy.returncode == 0, busy.stdout + busy.stderr  # not a failure
    assert box.git("rev-parse", "HEAD") == v1
    assert not (box.home / "deploy-failed").exists()
    assert _results(box)[-1] == "lock-busy"
    assert (box.home / "staged-ok").read_text().split()[0] == v2

    free = _tick(box)

    assert free.returncode == 0, free.stdout + free.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert _count(box, "uv run pytest -q") == 1, "the stamp from the first tick is reused"
    assert _count(box, "uv run ruff check .") == 1


def test_a_swap_that_rolls_back_fails_the_commit_once(tmp_path: Path) -> None:
    """The suite passed in staging but this environment cannot start: update.sh rolls back."""
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    result = _tick(box, FAKE_SMOKE_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert (box.home / "deploy-failed").read_text().strip() == v2
    assert "rolled back" in (box.home / "deploy-history.log").read_text()
    assert _results(box)[-1] == "swap-failed"

    again = _tick(box)
    assert again.returncode == 0
    assert _count(box, "uv run pytest -q") == 1


@pytest.mark.parametrize(
    "unit", ["argus-eval-models.service", "argus-reflect.service", "argus-backup.service"]
)
def test_a_job_that_takes_no_run_lock_defers_the_deploy(tmp_path: Path, unit: str) -> None:
    """eval-models, reflect, backup and the dashboard read or write the journal and the code
    without the run lock, so the lock alone cannot keep a swap from landing under them."""
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    box.promote()

    result = _tick(box, FAKE_ACTIVE_UNITS=unit)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert "uv " not in box.calls()
    assert _results(box)[-1] == "deferred"
    assert not (box.home / "deploy-failed").exists()


@pytest.mark.parametrize("state", ["activating", "active", "reloading", "deactivating"])
def test_a_job_in_any_running_state_defers_the_deploy(tmp_path: Path, state: str) -> None:
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    box.promote()

    result = _tick(box, FAKE_UNIT_STATES=f"argus-reflect.service={state}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert _results(box)[-1] == "deferred"


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_a_job_that_is_not_running_does_not_defer_the_deploy(tmp_path: Path, state: str) -> None:
    """`failed` is last night's result, not a job in progress: deferring on it would freeze the
    box until someone cleared an old failure."""
    box = _box(tmp_path)
    v2 = box.promote()

    result = _tick(box, FAKE_UNIT_STATES=f"argus-backup.service={state}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v2


def test_a_job_that_starts_while_the_suite_runs_still_defers_the_swap(tmp_path: Path) -> None:
    """Staging takes minutes; the 08:30 eval can start inside them. The check is repeated at the
    moment of the swap, and the work already done is kept."""
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()
    uv = box.bin / "uv"
    uv.write_text(
        uv.read_text().replace(
            'echo "uv $*" >> "$SANDBOX_LOG"',
            'echo "uv $*" >> "$SANDBOX_LOG"\n'
            'if [ "$1 $2" = "run pytest" ]; then '
            'echo argus-eval-models.service > "$SANDBOX_LOG.active"; fi',
            1,
        )
    )

    deferred = _tick(box)

    assert deferred.returncode == 0, deferred.stdout + deferred.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert _results(box)[-1] == "deferred"
    assert (box.home / "staged-ok").read_text().split()[0] == v2

    (box.root / "calls.log.active").unlink()
    later = _tick(box)

    assert later.returncode == 0, later.stdout + later.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert _count(box, "uv run pytest -q") == 1


# ── what the remote is allowed to say ────────────────────────────────────────────────────────


def _diverge_green(box: sandbox.Sandbox) -> str:
    """deploy/green is force-pushed to a commit that is not a descendant of what runs."""
    box.git(
        "checkout", "-q", "-b", "alt", box.git("rev-parse", "HEAD~1", cwd=box.other), cwd=box.other
    )
    (box.other / "app.txt").write_text("rewritten history\n")
    box.git("commit", "-q", "-am", "alt", cwd=box.other)
    sha = box.git("rev-parse", "HEAD", cwd=box.other)
    box.git("push", "-q", "--force", "origin", f"{sha}:refs/heads/deploy/green", cwd=box.other)
    return sha


def test_a_target_that_is_not_a_fast_forward_is_refused_and_pages_once(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v2 = _at_v2(box)
    alt = _diverge_green(box)

    first = _tick(box)

    assert first.returncode == 1, first.stdout + first.stderr
    assert "fast-forward" in first.stdout + first.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert "uv " not in box.calls()
    assert _results(box)[-1] == "refused-non-ff"

    second = _tick(box)

    assert second.returncode == 0, second.stdout + second.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert (box.home / "autodeploy.refused").read_text().strip() == alt


def test_a_later_fast_forward_target_is_accepted_after_a_refusal(tmp_path: Path) -> None:
    box = _box(tmp_path)
    _at_v2(box)
    _diverge_green(box)
    assert _tick(box).returncode == 1
    box.git("checkout", "-q", "main", cwd=box.other)
    v3 = box.push_main({"app.txt": "v3\n"}, "v3")
    box.git("push", "-q", "--force", "origin", f"{v3}:refs/heads/deploy/green", cwd=box.other)

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v3


def test_a_remote_that_stays_unreachable_pages_once_after_twelve_ticks(tmp_path: Path) -> None:
    """A blip is not an incident, but an hour of silence is: the box would stop updating and
    nothing would say so."""
    box = _box(tmp_path)
    box.promote()
    good_url = box.git("remote", "get-url", "origin")
    box.git("remote", "set-url", "origin", str(tmp_path / "nowhere"))

    codes = [_tick(box).returncode for _ in range(13)]

    assert codes == [0] * 11 + [1] + [0]
    assert box.calls() == ""

    box.git("remote", "set-url", "origin", good_url)
    assert _tick(box).returncode == 0
    assert not (box.home / "autodeploy.fetch-failures").exists(), "a success resets the count"
    assert box.git("rev-parse", "HEAD") == box.git("rev-parse", "HEAD", cwd=box.other)


# ── systemd units ────────────────────────────────────────────────────────────────────────────


def test_a_changed_unit_file_runs_the_root_owned_installer_with_no_arguments(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    v3 = box.push_main(
        {"deploy/systemd/argus-new.timer": "[Timer]\nOnCalendar=daily\n"}, "a new timer"
    )
    box.promote(v3)

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "sudo -n /usr/local/sbin/argus-install-units\n" in box.calls()
    assert box.git("rev-parse", "HEAD") == v3
    assert _results(box)[-1] == "deployed"


def test_a_deploy_that_changes_no_unit_does_not_call_the_installer(tmp_path: Path) -> None:
    box = _box(tmp_path)
    box.promote()
    assert _tick(box).returncode == 0
    assert "argus-install-units" not in box.calls()


def test_an_installer_that_fails_asks_the_owner_once_and_keeps_the_new_code(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v3 = box.push_main({"deploy/systemd/argus-new.timer": "[Timer]\n"}, "a new timer")
    box.promote(v3)

    first = _tick(box, FAKE_INSTALLER_EXIT="1")

    assert first.returncode == 1, first.stdout + first.stderr
    assert "OWNER ACTION: run sudo /opt/argus-project/deploy/grant-claude.sh" in (
        first.stdout + first.stderr
    )
    assert box.git("rev-parse", "HEAD") == v3, "the code is deployed either way"
    assert _results(box)[-1] == "owner-action"

    second = _tick(box)
    assert second.returncode == 0


def test_a_changed_installer_script_asks_the_owner_to_refresh_the_root_copy(tmp_path: Path) -> None:
    """The root-owned copy in /usr/local/sbin only changes when owner re-runs grant-claude.sh
    by hand,
    so a changed install-units.sh means the validator on the box is stale."""
    box = _box(tmp_path)
    v3 = box.push_main({"deploy/install-units.sh": "#!/usr/bin/env bash\n# v3\n"}, "installer")
    box.promote(v3)

    first = _tick(box)

    assert first.returncode == 1, first.stdout + first.stderr
    assert "OWNER ACTION" in first.stdout + first.stderr
    assert box.git("rev-parse", "HEAD") == v3
    assert _tick(box).returncode == 0


def test_the_deploy_never_asks_for_root_by_any_other_route(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v3 = box.push_main({"deploy/systemd/argus-new.timer": "[Timer]\n"}, "a new timer")
    box.promote(v3)

    _tick(box)

    sudo_calls = [ln for ln in box.calls().splitlines() if ln.startswith("sudo ")]
    assert set(sudo_calls) <= {
        "sudo -n /usr/local/sbin/argus-install-units",
        "sudo -n systemctl restart argus-ui.service",
    }, sudo_calls


# ── review round: verification, stamp refusals, timeouts, throttled pages, units pending ───────


def _manual_deploy_that_left_no_record(box: sandbox.Sandbox) -> str:
    """HEAD is the target, but nothing says a deploy of it ever finished (it died part-way)."""
    box.git("fetch", "-q", "origin")
    box.git("merge", "-q", "--ff-only", "origin/main")
    return box.git("rev-parse", "HEAD")


def _history(box: sandbox.Sandbox) -> str:
    path = box.home / "deploy-history.log"
    return path.read_text() if path.exists() else ""


def test_a_head_that_no_deploy_ever_finished_is_verified_once(tmp_path: Path) -> None:
    """`update.sh` died after the merge and before the end (the rollback bug): the checkout is on
    the new commit, the dependencies are half-synced, and every later tick saw HEAD == target and
    said 'up to date'. With no 'deployed' record for HEAD, run the swap once to re-verify."""
    box = _box(tmp_path)
    v2 = _manual_deploy_that_left_no_record(box)
    box.promote(v2)

    first = _tick(box)

    assert first.returncode == 0, first.stdout + first.stderr
    assert "re-verifying" in first.stdout
    assert f"{v2} -> {v2} deployed" in _history(box)
    assert _count(box, "uv run pytest -q") == 1, "staged once; the swap does not test again"
    assert _results(box)[-1] == "deployed"

    calls_before = box.calls()
    second = _tick(box)

    assert second.returncode == 0
    assert box.calls() == calls_before, "verified: nothing more to do"


def test_a_verification_that_fails_pages_once_and_does_not_loop(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v2 = _manual_deploy_that_left_no_record(box)
    box.promote(v2)

    first = _tick(box, FAKE_SMOKE_EXIT="1")

    assert first.returncode == 1, first.stdout + first.stderr
    assert (box.home / "autodeploy.verified").read_text().strip() == v2
    calls_after_first = box.calls()

    second = _tick(box)  # the environment would pass now; the attempt is remembered per HEAD

    assert second.returncode == 0
    assert box.calls() == calls_after_first


def test_a_head_with_a_deployed_record_is_not_verified_again(tmp_path: Path) -> None:
    box = _box(tmp_path)
    box.promote(_at_v2(box))  # _at_v2 writes the record a real update.sh would have
    assert _tick(box).returncode == 0
    assert box.calls() == ""


def test_a_stamp_update_sh_refuses_is_deleted_and_the_commit_staged_again(tmp_path: Path) -> None:
    """update.sh exits 76 when the stamp does not match, before touching anything — it never falls
    back to the suite under the locks. Autodeploy re-stages on the next tick."""
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    first = _tick(box, FAKE_BAD_HASH="1")

    assert first.returncode == 0, first.stdout + first.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert not (box.home / "staged-ok").exists()
    assert _results(box)[-1] == "stamp-mismatch"
    assert _count(box, "uv run pytest -q") == 1

    second = _tick(box)  # whatever disagreed is gone

    assert second.returncode == 0, second.stdout + second.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert _count(box, "uv run pytest -q") == 2, "re-staged, not trusted"


def test_a_stamp_that_is_refused_twice_for_one_commit_is_a_failure_not_a_loop(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    v2 = box.promote()

    assert _tick(box, FAKE_BAD_HASH="1").returncode == 0
    second = _tick(box, FAKE_BAD_HASH="1")

    assert second.returncode == 1, second.stdout + second.stderr
    assert (box.home / "deploy-failed").read_text().strip() == v2
    assert _tick(box, FAKE_BAD_HASH="1").returncode == 0
    assert _count(box, "uv run pytest -q") == 2


def test_the_staged_suite_runs_under_a_timeout_and_a_timeout_fails_the_commit(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    result = _tick(box, FAKE_TIMEOUT_PYTEST="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert (box.home / "deploy-failed").read_text().strip() == v2
    assert "timed out" in result.stdout
    (pytest_wrapper,) = [ln for ln in _timeouts(box).splitlines() if "pytest" in ln]
    seconds = int(pytest_wrapper.split()[3])
    assert 0 < seconds <= 1500


def test_a_tick_that_ran_long_does_not_swap_on_stale_information(tmp_path: Path) -> None:
    """Staging that overran its budget (the 30-minute unit timeout is the backstop): the target
    may have moved and a job may have started. Keep the stamp, swap on a fresh tick."""
    box = _box(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = box.promote()

    stale = _tick(box, ARGUS_AUTODEPLOY_TICK_MAX_S="-1")

    assert stale.returncode == 0, stale.stdout + stale.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert _results(box)[-1] == "stale-tick"
    assert (box.home / "staged-ok").read_text().split()[0] == v2

    fresh = _tick(box)

    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert _count(box, "uv run pytest -q") == 1


def test_uv_off_path_is_found_at_the_fallback_location(tmp_path: Path) -> None:
    """systemd gives the unit a bare PATH; uv lives in the argus user's ~/.local/bin. The fallback
    location is configurable so tests never depend on the machine they run on (F-161)."""
    box = _box(tmp_path)
    box.promote()
    moved = tmp_path / "home-local-bin-uv"
    (box.bin / "uv").rename(moved)

    result = _tick(
        box, PATH=f"{box.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", ARGUS_UV_FALLBACK=str(moved)
    )

    assert "uv not found" not in result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert _results(box)[-1] == "deployed"


def test_a_persistent_fault_pages_once_an_hour_not_every_tick(tmp_path: Path) -> None:
    """uv missing, flock missing, an unwritable state directory: exit 1 every five minutes is 288
    identical alerts a day. At most one per reason per hour."""
    box = _box(tmp_path)
    box.promote()
    fake_uv = box.bin / "uv"
    fake_uv.unlink()
    # The fallback must not find a real uv either: on the VPS, as argus, /home/argus/.local/bin/uv
    # exists, so without this the "missing" uv was found and ran (F-161).
    bare_path = {
        "PATH": f"{box.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "ARGUS_UV_FALLBACK": str(tmp_path / "no-uv-here"),
    }

    first = _tick(box, **bare_path)
    second = _tick(box, **bare_path)

    assert first.returncode == 1 and "uv not found" in first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert _results(box)[-1] == "paged-recently"

    (box.home / "autodeploy.alerted").write_text("uv-missing 0\n")  # an hour and more ago
    third = _tick(box, **bare_path)

    assert third.returncode == 1, third.stdout + third.stderr


def test_a_unit_install_that_fails_is_retried_each_tick_and_pages_once(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v3 = box.push_main({"deploy/systemd/argus-new.timer": "[Timer]\n"}, "a new timer")
    box.promote(v3)
    installer = "sudo -n /usr/local/sbin/argus-install-units"

    first = _tick(box, FAKE_INSTALLER_EXIT="1")

    assert first.returncode == 1, first.stdout + first.stderr
    assert (box.home / "autodeploy.units-pending").exists()
    assert box.git("rev-parse", "HEAD") == v3

    second = _tick(box, FAKE_INSTALLER_EXIT="1")  # still refused: retried, quietly

    assert second.returncode == 0, second.stdout + second.stderr
    assert box.calls().count(installer) == 2
    assert (box.home / "autodeploy.units-pending").exists()

    third = _tick(box)  # owner has fixed it

    assert third.returncode == 0, third.stdout + third.stderr
    assert box.calls().count(installer) == 3
    assert not (box.home / "autodeploy.units-pending").exists()
    assert _results(box)[-1] == "units-installed"

    assert _tick(box).returncode == 0
    assert box.calls().count(installer) == 3, "a cleared flag is not retried"


def test_a_new_commit_pages_again_while_the_installer_is_still_refused(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v3 = box.push_main({"deploy/systemd/argus-new.timer": "[Timer]\n"}, "a new timer")
    box.promote(v3)
    assert _tick(box, FAKE_INSTALLER_EXIT="1").returncode == 1
    assert _tick(box, FAKE_INSTALLER_EXIT="1").returncode == 0
    v4 = box.push_main({"deploy/systemd/argus-new.timer": "[Timer]\n# v4\n"}, "another unit change")
    box.promote(v4)

    third = _tick(box, FAKE_INSTALLER_EXIT="1")

    assert third.returncode == 1, "first failure for this target"
    assert box.git("rev-parse", "HEAD") == v4


def test_a_warning_on_stderr_does_not_become_the_remote_sha(tmp_path: Path) -> None:
    """ssh prints host-key warnings to stderr, which the lookup merges into its output so that a
    real failure can be logged. The sha is the line that is one, not the first word printed."""
    box = _box(tmp_path)
    box.promote(_at_v2(box))

    result = _tick(box, FAKE_LSREMOTE_NOISE="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _results(box) == ["up-to-date"]
    assert "git fetch" not in _timeouts(box)


# ── re-review: an unclean death, and the timing knobs ─────────────────────────────────────────


def test_every_bounded_step_escalates_to_a_kill_when_term_is_ignored(tmp_path: Path) -> None:
    """`timeout N cmd` only sends TERM, and a pytest that ignores it would hold the tick lock until
    the unit's 30 minutes ran out. `-k 30` sends KILL 30 seconds later."""
    box = _box(tmp_path)
    box.promote()
    assert _tick(box).returncode == 0

    lines = _timeouts(box).splitlines()
    assert lines and all(ln.startswith("timeout -k 30 ") for ln in lines), lines


def test_a_tick_that_ran_past_eleven_minutes_is_stale_by_default() -> None:
    """The unit allows 30 minutes in all; the swap itself may queue two minutes for two locks. A
    stage that finished with less than that left is not worth the risk."""
    text = (DEPLOY / "autodeploy.sh").read_text()
    assert "ARGUS_AUTODEPLOY_TICK_MAX_S:-1100" in text


def _dead_deploy(box: sandbox.Sandbox) -> tuple[str, str]:
    """A deploy of v2 killed with SIGKILL inside the swap. Returns (v1, v2)."""
    v1 = box.git("rev-parse", "HEAD")
    box.kill_9_during_the_migration()
    return v1, box.git("rev-parse", "HEAD")


def test_a_verification_that_fails_after_a_kill_9_ends_where_the_dead_deploy_started(
    tmp_path: Path,
) -> None:
    box = _box(tmp_path)
    v1, v2 = _dead_deploy(box)
    box.promote(v2)
    assert box.journal_rows() == ["before the deploy", "written during the deploy"]

    result = _tick(box, FAKE_SMOKE_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "recovering-interrupted-deploy" in _results(box)
    assert _results(box)[-1] == "swap-failed"
    assert box.git("rev-parse", "HEAD") == v1
    assert box.journal_rows() == ["before the deploy"]
    assert not (box.home / "deploy-inflight").exists()
    assert (box.home / "deploy-failed").read_text().strip() == v2


def test_a_marker_outranks_a_deployed_record_when_deciding_the_head_is_fine(
    tmp_path: Path,
) -> None:
    """The record says a deploy of HEAD finished at some point; the marker says one has died
    since. The newer fact wins: recover, do not report 'up to date'."""
    box = _box(tmp_path)
    v1, v2 = _dead_deploy(box)
    box.promote(v2)
    (box.home / "deploy-history.log").write_text(f"2026-10-11T00:00:00Z {v1} -> {v2} deployed\n")

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "recovering-interrupted-deploy" in _results(box)
    assert not (box.home / "deploy-inflight").exists()
    assert box.git("rev-parse", "HEAD") == v2


def test_a_verification_that_passes_after_a_kill_9_deploys_and_goes_quiet(tmp_path: Path) -> None:
    box = _box(tmp_path)
    v1, v2 = _dead_deploy(box)
    box.promote(v2)

    result = _tick(box)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{v1} -> {v2} deployed" in _history(box)
    assert not (box.home / "deploy-inflight").exists()
    calls = box.calls()
    assert _tick(box).returncode == 0
    assert box.calls() == calls, "recovered: nothing more to do"
