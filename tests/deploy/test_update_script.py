"""`deploy/update.sh` run for real, against a scratch repo and journal (see sandbox.py)."""

from __future__ import annotations

import fcntl
import hashlib
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from argus.runs.lock import LOCK_NAME, run_lock

from . import sandbox

UPDATE = sandbox.REPO / "deploy" / "update.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def test_the_deploy_guards_the_same_lock_file_the_runs_take() -> None:
    """P-N3: update.sh tested `$HOME_DIR/manager.lock`, a file no code ever creates; the runs
    lock `run.lock`. The guard could never fire, so a deploy could rewrite `.venv` underneath a
    manager in the middle of placing an order."""
    names = set(re.findall(r"\$HOME_DIR/([\w.-]+\.lock)", UPDATE.read_text()))
    # run.lock, and deploy.lock: the one the jobs that take no run lock hold shared (see the
    # units' ExecStart), so a swap cannot land under an eval, a reflection or a backup.
    assert names == {LOCK_NAME, "deploy.lock"}


def test_a_deploy_waits_for_a_running_trade_and_then_refuses(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    before = box.git("rev-parse", "HEAD")

    with run_lock("manager", box.home):
        result = box.run("update.sh", ARGUS_DEPLOY_LOCK_WAIT_S="1")

    # 75 (EX_TEMPFAIL), not 1: a busy lock is "try again", and the auto-deploy timer treats it
    # as such instead of raising a failure alert for what is just a trade in progress.
    assert result.returncode == 75, result.stdout + result.stderr
    assert "run is in progress" in result.stdout
    assert box.git("rev-parse", "HEAD") == before, "code was swapped under a running trade"
    assert "uv sync" not in box.calls()


def _probe_lock_during(box: sandbox.Sandbox, step: str, lock: str = LOCK_NAME) -> None:
    """Make the fake `uv <step>` try the named lock itself and record when it was free."""
    uv = box.bin / "uv"
    uv.write_text(
        uv.read_text().replace(
            'echo "uv $*" >> "$SANDBOX_LOG"',
            'echo "uv $*" >> "$SANDBOX_LOG"\n'
            f'if [ "$1" = {step} ]; then exec 8>>"$ARGUS_HOME/{lock}"; '
            f'flock -n 8 && echo "{lock} was free during {step}" >> "$SANDBOX_LOG"; fi',
        )
    )


def test_a_deploy_holds_the_lock_while_it_swaps_the_code(tmp_path: Path) -> None:
    """A run that starts mid-deploy must queue behind it, not import half-replaced modules."""
    box = sandbox.build(tmp_path)
    _probe_lock_during(box, "sync")

    result = box.run("update.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "uv sync" in box.calls()
    assert "lock was free during sync" not in box.calls()


def test_the_re_executed_newer_script_still_holds_the_lock(tmp_path: Path) -> None:
    """F-083's re-exec replaces the process; the lock must survive it, not lapse in between."""
    box = sandbox.build(tmp_path, v2_touches="update.sh")
    _probe_lock_during(box, "run")

    result = box.run("update.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "running the updated deploy script" in result.stdout
    assert "uv run pytest" in box.calls()
    assert "lock was free during run" not in box.calls()


def test_a_successful_deploy_moves_to_the_new_commit(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    result = box.run("update.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (box.checkout / "app.txt").read_text() == "v2\n"
    assert "deployed" in result.stdout


# ── P-N4: the journal rolls back with the code ───────────────────────────────────────────────

needs_sqlite3 = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="needs sqlite3 CLI")


@needs_sqlite3
def test_a_failed_deploy_restores_the_journal_it_started_with(tmp_path: Path) -> None:
    """P-N4: rollback reset the code and left the database alone. Migrations are forward-only
    with checksum enforcement, so old code would then run against a newer schema."""
    box = sandbox.build(tmp_path)
    before = box.git("rev-parse", "HEAD")

    result = box.run("update.sh", FAKE_TOUCH_JOURNAL="1", FAKE_PYTEST_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "rolling back" in result.stdout
    assert box.git("rev-parse", "HEAD") == before
    assert box.journal_rows() == ["before the deploy"]


@needs_sqlite3
def test_a_rollback_keeps_a_copy_of_the_journal_it_overwrites(tmp_path: Path) -> None:
    """F-160: a restore replaces the journal wholesale. After an interrupted deploy, runs on the
    half-deployed code may have written orders and snapshots before the recovery rolls back; those
    rows must survive somewhere, never be destroyed by the rollback itself."""
    box = sandbox.build(tmp_path)

    result = box.run("update.sh", FAKE_TOUCH_JOURNAL="1", FAKE_PYTEST_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert box.journal_rows() == ["before the deploy"]
    (kept,) = (box.home / "backup").glob("pre-rollback-*.db")
    with sqlite3.connect(kept) as conn:
        rows = [r[0] for r in conn.execute("SELECT v FROM t")]
    assert rows[0] == "before the deploy" and len(rows) == 2, rows


@needs_sqlite3
def test_every_deploy_leaves_a_snapshot_to_roll_back_to(tmp_path: Path) -> None:
    """The snapshot is what a later, manual rollback restores (local recovery procedure)."""
    box = sandbox.build(tmp_path)
    before = box.git("rev-parse", "HEAD")

    result = box.run("update.sh", FAKE_TOUCH_JOURNAL="1")

    assert result.returncode == 0, result.stdout + result.stderr
    (snapshot,) = (box.home / "backup").glob(f"pre-deploy-{before}-*.db")
    with sqlite3.connect(snapshot) as conn:
        assert [r[0] for r in conn.execute("SELECT v FROM t")] == ["before the deploy"]


@needs_sqlite3
def test_a_second_failed_deploy_from_the_same_commit_keeps_the_rows_written_since_the_first(
    tmp_path: Path,
) -> None:
    """Review 2026-10-07, second pass: the snapshot was named after the OLD sha alone and never
    retaken if it existed. Deploy A->B fails and keeps `pre-deploy-A.db`; the box trades on A for
    days; deploy A->C fails too and restores the days-old file — every order, stop and ledger row
    written in between, gone, under the message "journal restored"."""
    box = sandbox.build(tmp_path)

    assert box.run("update.sh", FAKE_PYTEST_EXIT="1").returncode == 1
    with sqlite3.connect(box.home / "journal.db") as conn:
        conn.execute("INSERT INTO t VALUES ('traded on A after the first failure')")

    result = box.run("update.sh", FAKE_TOUCH_JOURNAL="1", FAKE_PYTEST_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert box.journal_rows() == ["before the deploy", "traded on A after the first failure"]


@needs_sqlite3
def test_a_re_executed_deploy_restores_the_snapshot_its_first_copy_took(tmp_path: Path) -> None:
    """The newer copy must roll back to the journal as it was before the merge — the snapshot the
    first copy took, handed over by path — and not take a second one of its own."""
    box = sandbox.build(tmp_path, v2_touches="update.sh")

    result = box.run("update.sh", FAKE_TOUCH_JOURNAL="1", FAKE_PYTEST_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "running the updated deploy script" in result.stdout
    assert box.journal_rows() == ["before the deploy"]
    assert len(list((box.home / "backup").glob("pre-deploy-*.db"))) == 1


def test_a_box_with_no_journal_yet_still_deploys(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    (box.home / "journal.db").unlink()
    result = box.run("update.sh")
    assert result.returncode == 0, result.stdout + result.stderr


# ── P-N11: which commit is running, and which one it replaced ────────────────────────────────


def test_each_deploy_is_recorded_with_the_commit_it_replaced(tmp_path: Path) -> None:
    """There were no deployed-SHA tags: after a bad deploy, the commit to roll back to had to be
    dug out of the reflog on the box."""
    box = sandbox.build(tmp_path)
    old = box.git("rev-parse", "HEAD")

    assert box.run("update.sh").returncode == 0
    new = box.git("rev-parse", "HEAD")

    (line,) = (box.home / "deploy-history.log").read_text().splitlines()
    assert f"{old} -> {new} deployed" in line


def test_a_rolled_back_deploy_is_recorded_as_such(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    old = box.git("rev-parse", "HEAD")

    assert box.run("update.sh", FAKE_PYTEST_EXIT="1").returncode == 1

    (line,) = (box.home / "deploy-history.log").read_text().splitlines()
    assert old in line and "rolled back" in line


def test_the_journal_is_migrated_before_the_dashboard_is_restarted(tmp_path: Path) -> None:
    """The dashboard refuses a schema that is behind its code, so restarting it before anything
    has migrated would leave it crash-looping after a deploy that adds a migration."""
    box = sandbox.build(tmp_path)

    result = box.run("update.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    calls = box.calls()
    assert "open_journal" in calls
    assert calls.index("open_journal") < calls.index("systemctl restart argus-ui")


# ── hands-off deploys: --to, --pretested, the smoke, migrations under the trap ───────────────


def _v2(box: sandbox.Sandbox) -> str:
    return box.git("rev-parse", "HEAD", cwd=box.other)


def _stamp(box: sandbox.Sandbox, sha: str, lock: bytes = b"lock v1\n") -> None:
    """What autodeploy.sh writes once a commit has passed the tests in its staging checkout."""
    (box.home / "staged-ok").write_text(f"{sha} {hashlib.sha256(lock).hexdigest()}\n")


def test_to_deploys_exactly_that_commit_and_not_whatever_main_has_become(tmp_path: Path) -> None:
    """autodeploy follows its configured target; main may already be ahead."""
    box = sandbox.build(tmp_path)
    v2 = _v2(box)
    box.push_main({"app.txt": "v3\n"}, "v3, not yet green")

    result = box.run("update.sh", "--to", v2)

    assert result.returncode == 0, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v2
    assert (box.checkout / "app.txt").read_text() == "v2\n"


def test_to_refuses_to_move_the_checkout_backwards(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    assert box.run("update.sh").returncode == 0
    v2 = box.git("rev-parse", "HEAD")

    result = box.run("update.sh", "--to", v1)

    assert result.returncode != 0
    assert "fast-forward" in result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v2


def test_to_with_a_commit_the_checkout_cannot_find_changes_nothing(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    before = box.git("rev-parse", "HEAD")

    result = box.run("update.sh", "--to", "0" * 40)

    assert result.returncode != 0
    assert "unknown commit" in result.stdout
    assert box.git("rev-parse", "HEAD") == before
    assert "uv sync" not in box.calls()


@pytest.mark.parametrize("ref", ["main", "HEAD~1", "--upload-pack=x", "../x", ""])
def test_to_takes_a_sha_and_nothing_git_would_interpret(tmp_path: Path, ref: str) -> None:
    """`operator` may run update.sh as argus with any arguments; --to must not become a way to
    hand git
    a ref name, a revision expression or an option."""
    box = sandbox.build(tmp_path)
    before = box.git("rev-parse", "HEAD")

    result = box.run("update.sh", "--to", ref)

    assert result.returncode == 2, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == before
    assert box.calls() == ""


def test_an_unknown_argument_is_refused_before_anything_is_touched(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    result = box.run("update.sh", "--yolo")
    assert result.returncode == 2, result.stdout + result.stderr
    assert box.calls() == ""


def test_pretested_with_a_matching_stamp_skips_the_tests_but_not_the_smoke(tmp_path: Path) -> None:
    """The commit already passed the whole gate in the staging checkout, outside the run lock; the
    swap, which holds the lock, only proves that THIS environment imports and starts."""
    box = sandbox.build(tmp_path)
    v2 = _v2(box)
    _stamp(box, v2)

    result = box.run("update.sh", "--to", v2, "--pretested")

    assert result.returncode == 0, result.stdout + result.stderr
    calls = box.calls()
    assert "uv run pytest" not in calls
    assert "import argus.cli, argus.runs.manager, argus.runs.review, argus.runs.watchdog" in calls
    assert "uv run argus --help" in calls
    assert box.git("rev-parse", "HEAD") == v2


@pytest.mark.parametrize(
    "stamp",
    [
        pytest.param(lambda box, v2: None, id="no stamp"),
        pytest.param(lambda box, v2: _stamp(box, "f" * 40), id="stamp for another commit"),
        pytest.param(
            lambda box, v2: _stamp(box, v2, b"another lock\n"), id="stamp for another lock"
        ),
    ],
)
def test_pretested_without_a_matching_stamp_is_refused_before_anything_is_touched(
    tmp_path: Path, stamp: Callable[[sandbox.Sandbox, str], None]
) -> None:
    """`--pretested` is a claim, checked FIRST: before either lock, before the merge. It used to
    fall back to the whole suite under run.lock, which is exactly what the flag exists to avoid —
    minutes with the watchdog, manager and review queued behind a deploy. Exit 76, so autodeploy
    can tell it from a lock that was busy (75) and a deploy that failed (1), and re-stage."""
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = _v2(box)
    stamp(box, v2)

    result = box.run("update.sh", "--to", v2, "--pretested")

    assert result.returncode == 76, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == v1
    assert box.calls() == "", "no uv, no suite, nothing"
    assert not (box.home / "run.lock").exists() and not (box.home / "deploy.lock").exists()
    assert not (box.home / "deploy-history.log").exists()


def test_pretested_means_nothing_without_a_target(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    result = box.run("update.sh", "--pretested")
    assert result.returncode == 2, result.stdout + result.stderr
    assert box.calls() == ""


def test_a_manual_deploy_with_no_arguments_still_runs_the_full_gate(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    _stamp(box, _v2(box))  # a stamp does nothing unless --pretested asks for it
    assert box.run("update.sh").returncode == 0
    assert "uv run pytest" in box.calls()


def test_the_flags_survive_the_re_exec_into_the_newer_script(tmp_path: Path) -> None:
    """F-083's re-exec used to start the new copy with no arguments, which would have turned a
    pretested deploy of the box's own update.sh fix into a deploy of main."""
    box = sandbox.build(tmp_path, v2_touches="update.sh")
    v2 = _v2(box)
    box.push_main({"app.txt": "v3\n"}, "v3, not yet green")
    _stamp(box, v2)

    result = box.run("update.sh", "--to", v2, "--pretested")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "running the updated deploy script" in result.stdout
    assert box.git("rev-parse", "HEAD") == v2
    assert "uv run pytest" not in box.calls()


@needs_sqlite3
@pytest.mark.parametrize("failing", ["FAKE_SMOKE_EXIT", "FAKE_HELP_EXIT"])
def test_a_failed_smoke_rolls_back_the_deploy(tmp_path: Path, failing: str) -> None:
    """The staged tests ran in another venv. Either the entry points do not import here, or the CLI
    does not start here: the deploy is undone, not left for the next timer tick to find."""
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    _stamp(box, _v2(box))

    result = box.run("update.sh", "--to", _v2(box), "--pretested", **{failing: "1"})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "rolling back" in result.stdout
    assert box.git("rev-parse", "HEAD") == v1
    assert box.journal_rows() == ["before the deploy"]
    assert "systemctl restart argus-ui" not in box.calls()


@needs_sqlite3
def test_a_failed_migration_rolls_back_code_and_restores_the_journal(tmp_path: Path) -> None:
    """The migration ran AFTER the trap was disarmed, so a failing one printed a NOTE and the
    deploy 'succeeded' — new code on a half-migrated journal, the dashboard crash-looping."""
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")

    result = box.run("update.sh", FAKE_TOUCH_JOURNAL_MIGRATING="1", FAKE_MIGRATE_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "rolling back" in result.stdout
    assert box.git("rev-parse", "HEAD") == v1
    assert box.journal_rows() == ["before the deploy"]
    assert "systemctl restart argus-ui" not in box.calls()
    assert "rolled back" in (box.home / "deploy-history.log").read_text()


# ── review round: errtrace, signals, deploy.lock, OLD read under the locks ────────────────────


@contextmanager
def shared_hold(path: Path) -> Iterator[None]:
    with path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield


@needs_sqlite3
@pytest.mark.parametrize("v2_touches", ["", "update.sh"], ids=["plain", "re-exec"])
def test_a_sync_failure_after_the_merge_rolls_back_code_and_journal(
    tmp_path: Path, v2_touches: str
) -> None:
    """`trap rollback ERR` without errtrace (`set -E`) is not inherited into functions, and the
    post-merge `uv sync` is one (sync_env): errexit killed the script on the NEW commit with the
    old dependencies and no rollback. Unattended there is no human to see it and repair it."""
    box = sandbox.build(tmp_path, v2_touches=v2_touches)
    v1 = box.git("rev-parse", "HEAD")

    result = box.run("update.sh", FAKE_SYNC_EXIT="1", FAKE_TOUCH_JOURNAL_SYNCING="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "rolling back" in result.stdout
    assert box.git("rev-parse", "HEAD") == v1
    assert box.journal_rows() == ["before the deploy"]
    assert "rolled back" in (box.home / "deploy-history.log").read_text()


@needs_sqlite3
def test_a_function_that_fails_in_the_re_executed_copy_rolls_back_too(tmp_path: Path) -> None:
    """The re-exec path's own function, `snapshot_journal`: the first copy's snapshot is gone, so
    the second takes one, and sqlite3 fails. The trap is armed there; errexit alone would not
    have used it."""
    box = sandbox.build(tmp_path, v2_touches="update.sh")
    v1 = box.git("rev-parse", "HEAD")
    real = shutil.which("sqlite3")
    sandbox._write_exec(
        box.bin / "sqlite3",
        "#!/usr/bin/env bash\n"
        'if [ "${ARGUS_DEPLOY_REEXEC:-}" = 1 ] && [[ "$*" == *.backup* ]]; then\n'
        '  echo "sqlite3: simulated failure" >&2; exit 1; fi\n'
        f'exec "{real}" "$@"\n',
    )

    result = box.run("update.sh", FAKE_DELETE_SNAPSHOT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "running the updated deploy script" in result.stdout
    assert "rolling back" in result.stdout
    assert box.git("rev-parse", "HEAD") == v1


@needs_sqlite3
def test_sigterm_during_the_swap_rolls_back_instead_of_leaving_it_half_done(
    tmp_path: Path,
) -> None:
    """systemd's TimeoutStartSec, a `kill`, a reboot: the deploy gets SIGTERM between the merge
    and the end. Without a trap it dies where it stands — new code, journal migrated or not."""
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    hanging = box.root / "calls.log.hanging"
    env = {**box.env, "FAKE_HANG_MIGRATION": "1", "FAKE_TOUCH_JOURNAL_SYNCING": "1"}

    proc = subprocess.Popen(
        ["bash", str(box.checkout / "deploy" / "update.sh")],
        cwd=box.root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not hanging.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hanging.exists(), "the swap never reached the migration"
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=30)
    finally:
        proc.kill()

    assert proc.returncode == 1, output
    assert "rolling back" in output
    assert box.git("rev-parse", "HEAD") == v1
    assert box.journal_rows() == ["before the deploy"]


def test_a_deploy_waits_for_a_job_holding_deploy_lock_and_then_refuses(tmp_path: Path) -> None:
    """eval-models, reflect and backup hold deploy.lock shared (their units' ExecStart); the
    run lock alone could not keep a swap from landing under them."""
    box = sandbox.build(tmp_path)
    before = box.git("rev-parse", "HEAD")

    with shared_hold(box.home / "deploy.lock"):
        result = box.run("update.sh", ARGUS_DEPLOY_LOCK_WAIT_S="1")

    assert result.returncode == 75, result.stdout + result.stderr
    assert box.git("rev-parse", "HEAD") == before
    assert "uv sync" not in box.calls()


def test_a_busy_run_lock_and_a_busy_deploy_lock_are_told_apart(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    with shared_hold(box.home / "deploy.lock"):
        by_job = box.run("update.sh", ARGUS_DEPLOY_LOCK_WAIT_S="1")
    with run_lock("manager", box.home):
        by_trade = box.run("update.sh", ARGUS_DEPLOY_LOCK_WAIT_S="1")
    assert by_job.returncode == by_trade.returncode == 75
    assert "takes no run lock" in by_job.stdout
    assert "run is in progress" in by_trade.stdout


def test_deploy_lock_is_taken_before_the_run_lock_and_given_back(tmp_path: Path) -> None:
    """One order everywhere (deploy.lock, then run.lock), so two parties can never each hold the
    one the other wants."""
    text = UPDATE.read_text()
    assert text.index("take_deploy_lock\n") < text.index("take_run_lock\n")
    box = sandbox.build(tmp_path)
    with run_lock("manager", box.home):
        busy = box.run("update.sh", ARGUS_DEPLOY_LOCK_WAIT_S="1")
    assert busy.returncode == 75
    # The refused deploy exited, so it is not still holding deploy.lock: a job can start.
    with (box.home / "deploy.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_a_deploy_holds_deploy_lock_while_it_swaps_the_code(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    _probe_lock_during(box, "sync", "deploy.lock")

    result = box.run("update.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "uv sync" in box.calls()
    assert "deploy.lock was free during sync" not in box.calls()


def test_the_re_executed_newer_script_still_holds_deploy_lock(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path, v2_touches="update.sh")
    _probe_lock_during(box, "run", "deploy.lock")

    result = box.run("update.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "running the updated deploy script" in result.stdout
    assert "deploy.lock was free during run" not in box.calls()


def test_the_commit_a_deploy_replaces_is_read_after_the_locks_not_before(tmp_path: Path) -> None:
    """OLD was read first, then the deploy queued for the lock. A deploy that finished while it
    waited left HEAD somewhere else, and this one rolled back to — and recorded — a commit that
    was no longer the one it replaced."""
    box = sandbox.build(tmp_path)
    v2 = _v2(box)
    with run_lock("manager", box.home):
        proc = subprocess.Popen(
            ["bash", str(box.checkout / "deploy" / "update.sh")],
            cwd=box.root,
            env={**box.env, "ARGUS_DEPLOY_LOCK_WAIT_S": "20"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(1.0)  # it is queued on the lock by now
        box.git("fetch", "-q", "origin")
        box.git("merge", "-q", "--ff-only", "origin/main")  # another deploy lands meanwhile
    output, _ = proc.communicate(timeout=60)

    assert proc.returncode == 0, output
    assert f"already at {v2}" in output, output
    assert "updating" not in output


# ── an unclean death mid-swap: the rollback target must survive it (re-review M-A) ────────────

INFLIGHT = "deploy-inflight"
OLD_UPDATE_SH = Path(__file__).parent / "data" / "update_050b81f.sh"


def _kill_9_during_the_migration(box: sandbox.Sandbox, *args: str) -> None:
    box.kill_9_during_the_migration(*args)


@needs_sqlite3
def test_a_marker_names_what_a_dead_deploy_started_from(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")

    _kill_9_during_the_migration(box)

    assert box.git("rev-parse", "HEAD") == _v2(box), "the merge had happened"
    old, snapshot = (box.home / INFLIGHT).read_text().split()
    assert old == v1
    with sqlite3.connect(snapshot) as conn:
        assert [r[0] for r in conn.execute("SELECT v FROM t")] == ["before the deploy"]
    assert not list(box.home.glob(f"{INFLIGHT}.*")), "written atomically: no temp file is left"


@needs_sqlite3
def test_after_a_kill_9_a_failing_verification_rolls_back_to_where_the_dead_deploy_started(
    tmp_path: Path,
) -> None:
    """The verify-once path runs `update.sh --to HEAD`, which used to take OLD = HEAD — the bad
    commit itself — and a snapshot taken AFTER the dead deploy's migration. A failure then rolled
    back to the bad commit, restored the already-migrated journal, and called it a rollback."""
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = _v2(box)
    _kill_9_during_the_migration(box)
    assert box.journal_rows() == ["before the deploy", "written during the deploy"]
    _stamp(box, v2)

    result = box.run("update.sh", "--to", v2, "--pretested", FAKE_SMOKE_EXIT="1")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "rolling back to " + v1 in result.stdout
    assert box.git("rev-parse", "HEAD") == v1
    assert box.journal_rows() == ["before the deploy"]
    assert not (box.home / INFLIGHT).exists(), "a completed rollback clears the marker"


@needs_sqlite3
def test_after_a_kill_9_a_verification_that_passes_deploys_and_clears_the_marker(
    tmp_path: Path,
) -> None:
    box = sandbox.build(tmp_path)
    v1 = box.git("rev-parse", "HEAD")
    v2 = _v2(box)
    _kill_9_during_the_migration(box)
    _stamp(box, v2)

    result = box.run("update.sh", "--to", v2, "--pretested")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (box.home / INFLIGHT).exists()
    assert f"{v1} -> {v2} deployed" in (box.home / "deploy-history.log").read_text()


@needs_sqlite3
def test_a_manual_deploy_after_a_kill_9_runs_the_whole_gate_not_the_quick_verify(
    tmp_path: Path,
) -> None:
    """With a dead deploy on record, 'already at HEAD' is not a verification — nothing says HEAD
    was ever completed — so even by hand it goes through the full path (suite, smoke, migration)."""
    box = sandbox.build(tmp_path)
    _kill_9_during_the_migration(box)
    box.git("fetch", "-q", "origin")

    result = box.run("update.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already at" not in result.stdout
    assert "previous deploy died" in result.stdout
    assert "uv run pytest" in box.calls()
    assert "open_journal" in box.calls()
    assert not (box.home / INFLIGHT).exists()


@needs_sqlite3
@pytest.mark.parametrize("fails", [False, True], ids=["deployed", "rolled back"])
def test_no_deploy_that_finishes_leaves_a_marker_behind(tmp_path: Path, fails: bool) -> None:
    box = sandbox.build(tmp_path)
    env = {"FAKE_SMOKE_EXIT": "1"} if fails else {}

    result = box.run("update.sh", **env)

    assert result.returncode == (1 if fails else 0), result.stdout + result.stderr
    assert not (box.home / INFLIGHT).exists()


@needs_sqlite3
def test_the_marker_is_there_while_the_swap_runs(tmp_path: Path) -> None:
    box = sandbox.build(tmp_path)
    uv = box.bin / "uv"
    uv.write_text(
        uv.read_text().replace(
            'echo "uv $*" >> "$SANDBOX_LOG"',
            'echo "uv $*" >> "$SANDBOX_LOG"\n'
            'if [ "$1" = sync ] && [ -e "$ARGUS_HOME/deploy-inflight" ]; then '
            'echo "inflight marker present during sync" >> "$SANDBOX_LOG"; fi',
        )
    )

    assert box.run("update.sh").returncode == 0
    assert "inflight marker present during sync" in box.calls()


# ── L1: a lock that cannot be taken in the re-exec'd copy must not strand merged code ─────────


@needs_sqlite3
def test_a_transition_from_the_old_update_sh_rolls_back_if_the_new_lock_is_busy(
    tmp_path: Path,
) -> None:
    """The first deploy that meets deploy.lock is run by an update.sh that has never heard of it.
    It merges, syncs, and execs the new copy, which takes the lock the old one did not hand over.
    If a shared holder (an eval) is on it, that used to `exit 75` with the code already merged and
    nothing migrated or rolled back. The old script here is the real 050b81f one, vendored."""
    box = sandbox.build(
        tmp_path,
        v1_from={"update.sh": OLD_UPDATE_SH},
        v2_from={"update.sh": sandbox.REPO / "deploy" / "update.sh"},
    )
    v1 = box.git("rev-parse", "HEAD")

    with shared_hold(box.home / "deploy.lock"):
        result = box.run("update.sh", ARGUS_DEPLOY_LOCK_WAIT_S="2")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "running the updated deploy script" in result.stdout
    assert "rolling back" in result.stdout
    assert box.git("rev-parse", "HEAD") == v1
    assert "open_journal" not in box.calls()
    assert box.journal_rows() == ["before the deploy"]


@needs_sqlite3
def test_the_same_transition_deploys_when_nothing_holds_the_lock(tmp_path: Path) -> None:
    box = sandbox.build(
        tmp_path,
        v1_from={"update.sh": OLD_UPDATE_SH},
        v2_from={"update.sh": sandbox.REPO / "deploy" / "update.sh"},
    )

    result = box.run("update.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "running the updated deploy script" in result.stdout
    assert box.git("rev-parse", "HEAD") == _v2(box)


@needs_sqlite3
def test_a_deploy_started_by_the_old_update_sh_is_covered_from_the_re_exec_on(
    tmp_path: Path,
) -> None:
    """The old script writes no marker. Its newer self, once exec'd, must write one — the dead
    deploy of the first auto-deploy-era commit is exactly the one nobody has yet protected."""
    box = sandbox.build(
        tmp_path,
        v1_from={"update.sh": OLD_UPDATE_SH},
        v2_from={"update.sh": sandbox.REPO / "deploy" / "update.sh"},
    )
    v1 = box.git("rev-parse", "HEAD")

    _kill_9_during_the_migration(box)

    old, snapshot = (box.home / INFLIGHT).read_text().split()
    assert old == v1
    assert Path(snapshot).is_file()
