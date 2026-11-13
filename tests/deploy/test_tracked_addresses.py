"""No tracked file carries the box's address (review 2026-09-18 P-16, PARTIAL on 2026-10-07).

The VPS's public IP sat in `deploy/dashboard.sh` (as the tunnel's default host) and in a test
docstring, and the tailnet address in RUNBOOK. Once the repo is pushed anywhere, that is the
address of the machine holding the trading keys, next to a description of what runs on it. The
address must be supplied separately in `ARGUS_HOST`. Documentation ranges (RFC 5737), private
and loopback addresses are fine.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .systemd_units import DEPLOY, REPO

NOT_SCANNED = {"uv.lock"}
TAILNET = ipaddress.ip_network("100.64.0.0/10")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")


def _tracked() -> list[Path]:
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs a git checkout")
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPO / name
        for name in set(out.splitlines())
        if name not in NOT_SCANNED and (REPO / name).is_file()
    ]


def test_public_candidate_scan_includes_new_files_and_excludes_ignored_and_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("private.txt\n")
    (tmp_path / "old.txt").write_text("old")
    subprocess.run(["git", "add", "old.txt"], cwd=tmp_path, check=True)
    (tmp_path / "old.txt").unlink()
    (tmp_path / "new.txt").write_text("8.8." + "8.8")
    (tmp_path / "private.txt").write_text("8.8." + "8.8")
    monkeypatch.setitem(globals(), "REPO", tmp_path)
    candidates = _tracked()
    assert tmp_path / "new.txt" in candidates
    assert _leaks(tmp_path / "new.txt", (tmp_path / "new.txt").read_text())
    assert tmp_path / "private.txt" not in candidates
    assert tmp_path / "old.txt" not in candidates


def _leaks(path: Path, text: str) -> list[str]:
    found = []
    for raw in IPV4.findall(text):
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue  # 999.1.2.3 is a version string, not an address
        if address.is_global:
            found.append(raw)
        elif address in TAILNET and "tests" not in path.relative_to(REPO).parts:
            found.append(raw)  # tests may invent a tailnet-shaped sample; docs and scripts may not
    return found


def test_no_tracked_file_names_a_public_or_tailnet_address() -> None:
    leaks = {}
    for path in _tracked():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if found := _leaks(path, text):
            leaks[str(path.relative_to(REPO))] = found
    assert not leaks, leaks


def test_the_scanner_catches_a_public_address() -> None:
    sample = REPO / "deploy" / "x.sh"
    public = ".".join(["8", "8", "4", "4"])  # assembled, so this file does not trip its own scan
    assert _leaks(sample, f"HOST=user@{public}") == [public]
    assert _leaks(sample, "a tailnet 100.101.102.103 bind") == ["100.101.102.103"]
    assert _leaks(sample, "203.0.113.7 192.168.1.5 127.0.0.1 0.0.0.0 version 2.29.0") == []


def _fake_bin(tmp_path: Path, resolved_host: str) -> dict[str, str]:
    """`ssh -G` resolves to `resolved_host`; a tunnel attempt is recorded and ends the script."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ssh.log"
    (bin_dir / "ssh").write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = "-G" ]; then echo "user user"; echo "hostname {resolved_host}"; exit 0; fi\n'
        f'echo "$*" >> "{log}"\n'
        "kill -TERM $PPID\n"
    )
    (bin_dir / "lsof").write_text("#!/usr/bin/env bash\nexit 1\n")
    for tool in ("ssh", "lsof"):
        (bin_dir / tool).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARGUS_")}
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ARGUS_PORT"] = "18787"
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY / "dashboard.sh")], env=env, capture_output=True, text=True, timeout=20
    )


def test_dashboard_without_a_host_refuses_to_connect(tmp_path: Path) -> None:
    env = _fake_bin(tmp_path, resolved_host="unused")
    result = _run(env)
    assert result.returncode == 2
    assert "ARGUS_HOST" in result.stdout + result.stderr
    assert not (tmp_path / "ssh.log").exists()


def test_dashboard_tunnels_through_the_alias(tmp_path: Path) -> None:
    env = _fake_bin(tmp_path, resolved_host="203.0.113.10")
    env["ARGUS_HOST"] = "example-alias"
    _run(env)
    assert "example-alias" in (tmp_path / "ssh.log").read_text()


def test_dashboard_honours_an_explicit_host(tmp_path: Path) -> None:
    env = _fake_bin(tmp_path, resolved_host="never-asked")
    env["ARGUS_HOST"] = "user@203.0.113.11"
    _run(env)
    assert "user@203.0.113.11" in (tmp_path / "ssh.log").read_text()


def test_dashboard_passes_an_explicit_key(tmp_path: Path) -> None:
    env = _fake_bin(tmp_path, resolved_host="unused")
    env["ARGUS_HOST"] = "example-alias"
    env["ARGUS_KEY"] = "/tmp/example-key"
    _run(env)
    assert "-i /tmp/example-key" in (tmp_path / "ssh.log").read_text()


@pytest.mark.parametrize("host", ["-oProxyCommand=bad", "user host", "user\thost"])
def test_dashboard_rejects_unsafe_host_before_ssh(tmp_path: Path, host: str) -> None:
    env = _fake_bin(tmp_path, resolved_host="unused")
    env["ARGUS_HOST"] = host
    result = _run(env)
    assert result.returncode != 0
    assert not (tmp_path / "ssh.log").exists()


@pytest.mark.parametrize("port", ["0", "65536", "-1", "22x", "123456"])
def test_dashboard_rejects_invalid_port_before_ssh(tmp_path: Path, port: str) -> None:
    env = _fake_bin(tmp_path, resolved_host="unused")
    env["ARGUS_HOST"] = "example-alias"
    env["ARGUS_PORT"] = port
    result = _run(env)
    assert result.returncode != 0
    assert not (tmp_path / "ssh.log").exists()
