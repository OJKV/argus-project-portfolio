"""The units that run deploy/autodeploy.sh, and what they are and are not allowed to be.

The service is the one ARGUS unit that has to `sudo`: it restarts the dashboard and runs the
root-owned unit installer. Everything else about it is kept as small as the other housekeeping
units — no EnvironmentFile (the staged test suite runs under this unit and must not inherit the
live keys), a timeout that covers a cold `uv sync` plus the suite on two busy cores, and an
OnFailure alert, because the whole point is that a human hears about a deploy that did not land.
"""

from __future__ import annotations

import subprocess

import pytest

from .systemd_units import DEPLOY, SYSTEMD, unit_values

SERVICE = "argus-autodeploy.service"
TIMER = "argus-autodeploy.timer"


def test_the_service_runs_the_script_as_argus_in_a_clean_environment() -> None:
    assert unit_values(SERVICE, "Type") == ["oneshot"]
    assert unit_values(SERVICE, "User") == ["argus"]
    assert unit_values(SERVICE, "ExecStart") == ["/opt/argus-project/deploy/autodeploy.sh"]
    assert unit_values(SERVICE, "Environment") == ["ARGUS_HOME=/var/lib/argus"]
    assert unit_values(SERVICE, "EnvironmentFile") == [], "tests must not see secrets"


def test_a_failed_deploy_alerts() -> None:
    assert unit_values(SERVICE, "OnFailure") == ["argus-failure@%n.service"]


def test_the_timeout_covers_a_cold_sync_and_the_suite_on_a_busy_box() -> None:
    assert unit_values(SERVICE, "TimeoutStartSec") == ["1800"]


def test_the_timer_polls_every_five_minutes_starting_soon_after_boot() -> None:
    assert unit_values(TIMER, "OnBootSec") == ["5min"]
    assert unit_values(TIMER, "OnUnitActiveSec") == ["5min"]
    assert unit_values(TIMER, "WantedBy") == ["timers.target"]


def test_the_script_the_unit_starts_exists_and_is_executable() -> None:
    script = DEPLOY / "autodeploy.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "systemd execs it directly"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


def test_the_service_unit_passes_the_installers_own_validator() -> None:
    result = subprocess.run(
        ["bash", str(DEPLOY / "install-units.sh"), "--check", str(SYSTEMD)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── deploy.lock: the jobs that take no run lock ────────────────────────────────────────────────

SHARED = "/usr/bin/flock -s -w 600 /var/lib/argus/deploy.lock "
NO_RUN_LOCK_JOBS = {
    "argus-eval-models.service": "/opt/argus-project/.venv/bin/argus eval-models",
    "argus-reflect.service": "/opt/argus-project/.venv/bin/argus reflect",
    "argus-backup.service": "/opt/argus-project/deploy/backup.sh",
}


def test_the_service_runs_at_normal_priority_so_the_swap_is_not_starved() -> None:
    """Staging is wrapped in `nice -n 19 ionice -c3` by the script itself; the swap holds the
    locks and must not be queued behind the other bot's CPU use."""
    assert unit_values(SERVICE, "Nice") == ["0"]


def test_the_unit_says_why_it_may_sudo_and_what_that_costs() -> None:
    comments = "\n".join(
        ln for ln in (SYSTEMD / SERVICE).read_text().splitlines() if ln.startswith("#")
    )
    assert "systemd.exec(5)" in comments and "no_new_privs" in comments


@pytest.mark.parametrize("unit", sorted(NO_RUN_LOCK_JOBS))
def test_a_job_that_takes_no_run_lock_holds_deploy_lock_shared(unit: str) -> None:
    """update.sh takes deploy.lock exclusively, so a swap cannot land under one of these jobs and
    one cannot start under a swap — closing the window between autodeploy's check and its swap.
    The same form argus-live-keys.service uses for run.lock, so the root-owned validator accepts
    it (`--check` above)."""
    (exec_start,) = unit_values(unit, "ExecStart")
    assert exec_start == SHARED + NO_RUN_LOCK_JOBS[unit]


def test_the_units_and_update_sh_name_the_same_lock_file() -> None:
    assert "$HOME_DIR/deploy.lock" in (DEPLOY / "update.sh").read_text()
    assert SHARED.split()[4] == "/var/lib/argus/deploy.lock"


@pytest.mark.parametrize(
    ("unit", "ran_for"), [("argus-eval-models.service", 3600), ("argus-reflect.service", 900)]
)
def test_the_timeout_covers_the_wait_for_deploy_lock_as_well_as_the_job(
    unit: str, ran_for: int
) -> None:
    """flock -w 600 queues behind a swap for up to ten minutes before the job starts."""
    (timeout,) = unit_values(unit, "TimeoutStartSec")
    assert int(timeout) >= ran_for + 600


def test_no_other_unit_takes_deploy_lock() -> None:
    """One order everywhere: deploy.lock, then run.lock. Only update.sh takes both, and the jobs
    above take only the first, so nobody can hold run.lock and wait for deploy.lock."""
    holders = {
        p.name
        for p in SYSTEMD.glob("*.service")
        if "deploy.lock" in " ".join(unit_values(p.name, "ExecStart"))
    }
    assert holders == set(NO_RUN_LOCK_JOBS)


def test_a_rollback_started_by_term_gets_time_to_finish() -> None:
    """update.sh rolls back on SIGTERM — a systemd timeout, a stop — by resetting the code, running
    `uv sync` and restoring the journal. The default 90 s TimeoutStopSec would KILL it half-way,
    which is the one outcome the rollback exists to prevent."""
    assert unit_values(SERVICE, "TimeoutStopSec") == ["300"]
