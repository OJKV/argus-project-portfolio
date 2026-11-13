"""The password-free unit installer cannot be turned into root (review 2026-10-07, P-N1).

`grant-claude.sh` let `operator` run the repo's `install-units.sh` as root with no password.
That script
copied whatever `deploy/systemd/*.service` said into /etc/systemd/system and started it — and the
repo is writable by `argus`, the user every run already is. A unit saying `User=root`, or an
`ExecStart=+…` (the "+" runs that line with full privileges), turned one pushed commit into root,
`.env`, and an armed run with no human in the loop.

owner wants the automation kept, so it is made safe instead of removed: sudo now names a root-owned
copy of the installer outside the repo, and that copy refuses any unit that could run as root.
`--check DIR` runs the same validation unprivileged, which is what these tests drive.
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .systemd_units import DEPLOY, SYSTEMD

INSTALLER = DEPLOY / "install-units.sh"
GRANT = DEPLOY / "grant-claude.sh"
SBIN = "/usr/local/sbin/argus-install-units"


def _check(units: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER), "--check", str(units)],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def units(tmp_path: Path) -> Path:
    """A copy of the shipped units to tamper with."""
    target = tmp_path / "systemd"
    shutil.copytree(SYSTEMD, target)
    return target


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{path.name}: {old!r} not found"
    path.write_text(text.replace(old, new, 1))


def test_the_shipped_units_pass() -> None:
    result = _check(SYSTEMD)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "units OK" in result.stdout


MANAGER = "argus-manager.service"
EXEC = "ExecStart=/opt/argus-project/.venv/bin/argus manager --llm"

TAMPERED = {
    "runs as root": (MANAGER, "User=argus", "User=root"),
    "no User= at all": (MANAGER, "User=argus\n", ""),
    "User= reset to empty (systemd's default is root)": (
        MANAGER,
        "User=argus",
        "User=argus\nUser=",
    ),
    "root group": (MANAGER, "User=argus", "User=argus\nGroup=root"),
    "+ prefix runs the line as root": (MANAGER, EXEC, EXEC.replace("=/", "=+/")),
    "! prefix skips the user switch": (MANAGER, EXEC, EXEC.replace("=/", "=!/")),
    "!! prefix": (MANAGER, EXEC, EXEC.replace("=/", "=!!/")),
    "combined -+ prefix": (MANAGER, EXEC, EXEC.replace("=/", "=-+/")),
    "a privileged pre-step": (MANAGER, EXEC, f"ExecStartPre=+/bin/true\n{EXEC}"),
    "PermissionsStartOnly": (MANAGER, "User=argus", "User=argus\nPermissionsStartOnly=true"),
    "AmbientCapabilities": (MANAGER, "User=argus", "User=argus\nAmbientCapabilities=CAP_SYS_ADMIN"),
    "CapabilityBoundingSet": (
        MANAGER,
        "User=argus",
        "User=argus\nCapabilityBoundingSet=CAP_SYS_ADMIN",
    ),
    "supplementary groups": (MANAGER, "User=argus", "User=argus\nSupplementaryGroups=sudo"),
    "root reads any file for the unit": (
        MANAGER,
        "User=argus",
        "User=argus\nLoadCredential=k:/etc/shadow",
    ),
    "output file opened by PID 1": (
        MANAGER,
        "User=argus",
        "User=argus\nStandardOutput=file:/etc/x",
    ),
    "environment from a root-only file": (
        MANAGER,
        "EnvironmentFile=/opt/argus-project/.env",
        "EnvironmentFile=/root/.env",
    ),
    "starts a system unit on failure": (MANAGER, "[Service]", "OnFailure=reboot.target\n[Service]"),
    "negative nice": (MANAGER, "Nice=5", "Nice=-20"),
    "line continuation hides a key": (MANAGER, "User=argus", "Description=x \\\nUser=argus"),
    "a timer that starts a system unit": (
        "argus-review.timer",
        "[Timer]",
        "[Timer]\nUnit=ssh.service",
    ),
    "an alias over a system unit": (
        "argus-review.timer",
        "[Install]",
        "[Install]\nAlias=sshd.service",
    ),
}


@pytest.mark.parametrize("case", sorted(TAMPERED))
def test_a_unit_that_could_run_as_root_is_refused(units: Path, case: str) -> None:
    name, old, new = TAMPERED[case]
    _edit(units / name, old, new)
    result = _check(units)
    assert result.returncode != 0, case
    assert "refusing" in result.stdout + result.stderr, case
    assert name in result.stdout + result.stderr, case


# Review 2026-10-07 (second pass): systemd reads User= only in [Service] and ignores it, with a log
# line, anywhere else. Counting `User=argus` lines without their section passed a unit whose only
# User= sat under [Unit] — and that unit ran as root.
MISPLACED = {
    "User= only under [Unit]": (
        "argus-evil.service",
        "[Unit]\nDescription=x\nUser=argus\n"
        "[Service]\nType=oneshot\nExecStart=/bin/sh -c 'id > /tmp/whoami'\n",
    ),
    "User= only under [Install]": (
        "argus-evil.service",
        "[Unit]\nDescription=x\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
        "[Install]\nUser=argus\nWantedBy=multi-user.target\n",
    ),
    "User= before any section": (
        "argus-evil.service",
        "User=argus\n[Unit]\nDescription=x\n[Service]\nExecStart=/bin/true\n",
    ),
    "a second User= line": (
        "argus-evil.service",
        "[Service]\nUser=argus\nUser=argus\nExecStart=/bin/true\n",
    ),
    "a [Timer] section in a service": (
        "argus-evil.service",
        "[Service]\nUser=argus\nExecStart=/bin/true\n[Timer]\nOnCalendar=daily\n",
    ),
    "a [Service] section in a timer": (
        "argus-evil.timer",
        "[Timer]\nOnCalendar=daily\n[Service]\nExecStart=/bin/true\n",
    ),
    "a service key under [Unit]": (
        "argus-evil.service",
        "[Unit]\nEnvironmentFile=/opt/argus-project/.env\n"
        "[Service]\nUser=argus\nExecStart=/bin/true\n",
    ),
}


@pytest.mark.parametrize("case", sorted(MISPLACED))
def test_a_key_in_the_wrong_section_is_refused(units: Path, case: str) -> None:
    name, body = MISPLACED[case]
    (units / name).write_text(body)
    result = _check(units)
    assert result.returncode != 0, case
    assert "refusing" in result.stdout + result.stderr, case
    assert name in result.stdout + result.stderr, case


def test_the_shipped_manager_with_its_user_moved_to_unit_is_refused(units: Path) -> None:
    """The reviewer's reproduction on a real unit: still one `User=argus` line, wrong section."""
    _edit(units / MANAGER, "User=argus\n", "")
    _edit(units / MANAGER, "[Unit]\n", "[Unit]\nUser=argus\n")
    result = _check(units)
    assert result.returncode != 0
    assert MANAGER in result.stdout + result.stderr


def test_a_unit_not_named_argus_is_refused(units: Path) -> None:
    shutil.copy(units / MANAGER, units / "sshd.service")
    result = _check(units)
    assert result.returncode != 0
    assert "sshd.service" in result.stdout + result.stderr


def test_a_symlinked_unit_is_refused(units: Path, tmp_path: Path) -> None:
    """Copying as root follows a symlink — to /etc/shadow, say — and installs it world-readable."""
    secret = tmp_path / "secret"
    secret.write_text("[Service]\nUser=argus\n")
    (units / "argus-extra.service").symlink_to(secret)
    result = _check(units)
    assert result.returncode != 0
    assert "argus-extra.service" in result.stdout + result.stderr


# ── how sudo reaches it ───────────────────────────────────────────────────────────────────────


def _grant_preview(username: str | None = None) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(GRANT), "--print-rules"]
    if username is not None:
        args.append(username)
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _granted_rules() -> list[str]:
    result = _grant_preview("sample_operator")
    assert result.returncode == 0, result.stderr
    return [ln for ln in result.stdout.splitlines() if ln.strip() and not ln.startswith("#")]


@pytest.mark.parametrize(
    "username",
    [None, "", "-x", "root", "argus", "nobody", "daemon", "root ALL=(ALL) ALL", "name;id", "a/b"],
)
def test_grant_preview_rejects_missing_or_unsafe_username(username: str | None) -> None:
    result = _grant_preview(username)
    assert result.returncode != 0
    assert "NOPASSWD:" not in result.stdout


def test_grant_install_refuses_nonexistent_account_before_privilege_check(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_id = bin_dir / "id"
    fake_id.write_text("#!/bin/sh\nexit 1\n")
    fake_id.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", str(GRANT), "sample_operator"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unknown operator" in result.stderr


@pytest.mark.parametrize(
    "uid,shell,message",
    [
        ("99", "/bin/bash", "system account"),
        ("1001", "/usr/sbin/nologin", "login shell"),
        ("1001", "/bin/bash", "run with sudo"),
    ],
)
def test_grant_install_checks_identity_without_installing(
    tmp_path: Path, uid: str, shell: str, message: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_id = bin_dir / "id"
    fake_id.write_text(f'#!/bin/sh\nif [ "$#" -eq 2 ]; then echo {uid}; else echo 1001; fi\n')
    fake_getent = bin_dir / "getent"
    fake_getent.write_text(f"#!/bin/sh\necho 'sample_operator:x:{uid}:1001::/tmp:{shell}'\n")
    fake_id.chmod(0o755)
    fake_getent.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    result = subprocess.run(
        ["bash", str(GRANT), "sample_operator"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert message in result.stderr


def test_grant_preview_uses_only_the_requested_synthetic_operator() -> None:
    rules = _granted_rules()
    user_rules = [rule for rule in rules if rule.startswith("sample_operator ")]
    assert len(user_rules) == 3
    assert all("NOPASSWD:" in rule for rule in user_rules)


def test_root_is_granted_only_for_the_root_owned_installer() -> None:
    as_root = [rule for rule in _granted_rules() if "(root)" in rule]
    assert as_root, "no root rule left — then nothing installs units without a password"
    for rule in as_root:
        command = rule.split("NOPASSWD:", 1)[1].strip()
        assert command in (f'{SBIN} ""', "/usr/bin/systemctl restart argus-ui.service"), rule


def test_smoking_a_unit_still_needs_a_password() -> None:
    """Review 2026-10-07, second pass: the rule allowed any arguments, so a password-free
    `--smoke argus-manager.service` started an armed manager on whatever account .env names —
    live included — around trade.sh's demo-only check, and every extra run can add plans past the
    heat cap while the gate judges an empty book (G-N1). In sudoers, `""` after a command means
    "no arguments"; `--smoke` then needs owner's password, i.e. owner. Holds for every user
    granted it."""
    rules = [r for r in _granted_rules() if SBIN in r]
    assert rules, "no rule names the installer"
    for rule in rules:
        assert rule.rstrip().endswith(f'{SBIN} ""'), rule


def test_the_argus_user_may_run_the_installer_with_no_arguments_for_autodeploy() -> None:
    """deploy/autodeploy.sh runs as `argus` and applies a changed unit file by itself. It gets the
    same rule `operator` has — the root-owned, validating copy, no arguments — and no wider one."""
    assert [
        r for r in _granted_rules() if r.startswith("argus ") and "(root)" in r and SBIN in r
    ] == [f'argus ALL=(root) NOPASSWD: {SBIN} ""']


def test_the_grant_header_documents_the_autodeploy_rule_and_the_real_gap() -> None:
    header = GRANT.read_text().split("set -euo pipefail")[0]
    assert re.search(r"^#\s+5\. argus -> .*install", header, re.M), "rule 5 is undocumented"
    assert "Grants five things" in header
    assert "CI has no deployment-promotion job" in header
    assert "not proof of a passing CI run" in header
    assert "deploy/green" in header


def test_nothing_in_the_argus_writable_checkout_runs_as_root() -> None:
    for rule in _granted_rules():
        if "(root)" in rule:
            assert "/opt/argus-project" not in rule, rule


def test_the_grant_installs_a_root_owned_copy_of_the_installer() -> None:
    text = GRANT.read_text()
    assert "install -m 755 -o root -g root" in text and SBIN in text


def test_the_root_copy_reads_units_from_the_fixed_path_not_its_own_directory() -> None:
    """Run from /usr/local/sbin, `dirname $0` would be meaningless; run with a caller-chosen
    directory, it would install the caller's units."""
    text = INSTALLER.read_text()
    assert re.search(r"^UNIT_SRC=/opt/argus-project/deploy/systemd$", text, re.M)
    assert 'cd "$(dirname "$0")"' not in text


def test_installing_needs_root(units: Path) -> None:
    # Decided BEFORE the installer runs: as root it is not a test of "refuses without root" but the
    # real installer, installing units into /etc/systemd/system on the machine running the suite.
    if os.geteuid() == 0:
        pytest.skip("running as root")
    result = subprocess.run(["bash", str(INSTALLER)], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "root" in result.stdout + result.stderr


def test_the_needs_root_test_decides_before_it_runs_the_installer() -> None:
    """Run as root it would not test a refusal, it would install units on the machine under test."""
    source = inspect.getsource(test_installing_needs_root)
    assert source.index("geteuid") < source.index("subprocess.run")
