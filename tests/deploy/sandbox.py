"""A throwaway box for running the real deploy scripts: a git origin, a checkout, fake tools.

The deploy scripts are the one part of ARGUS no Python test imports, and every defect in them so
far was found on the VPS (F-078, F-081, F-082, F-083). Running the real script against a scratch
git repo with `uv`, `sudo` and `systemctl` replaced by recorders catches the same class of bug
here instead. Nothing in this module touches the network, systemd or the real journal.
"""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# A real `flock` exists on the VPS and on the CI runner (util-linux), not on macOS. The shim does
# what `flock [-n|-w secs] FD` does — flock(2) on an inherited descriptor, which then stays
# locked for the parent shell after the shim exits — so the scripts run unchanged on a laptop.
FLOCK_SHIM = f"""#!{sys.executable}
import fcntl, sys, time
args = sys.argv[1:]
wait = 0.0 if "-n" in args else None
if "-w" in args:
    wait = float(args[args.index("-w") + 1])
fd = int(args[-1])
deadline = None if wait is None else time.monotonic() + wait
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if deadline is not None and time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.05)
"""

# `uv sync` succeeds; `uv run pytest` can be told to fail, and to write to the journal first —
# standing in for anything that touches the schema during a deploy. The post-swap smoke imports
# and the journal migration are separate `uv run python -c` calls with a switch each, so a test
# can fail exactly one step of the deploy. Every call also logs where it ran and with which
# ARGUS_HOME (the `ctx:` line), which is how a test proves the staged tests never saw the journal.
FAKE_UV = f"""#!/usr/bin/env bash
echo "uv $*" >> "$SANDBOX_LOG"
echo "ctx:$1:$2 pwd=$PWD ARGUS_HOME=$ARGUS_HOME" >> "$SANDBOX_LOG"
touch_journal() {{
  "{sys.executable}" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); \\
c.execute(\\"INSERT INTO t VALUES ('written during the deploy')\\"); c.commit()" \\
    "$ARGUS_HOME/journal.db"
}}
if [ "$1" = run ] && [ "$2" = pytest ]; then
  if [ -n "${{FAKE_TOUCH_JOURNAL:-}}" ]; then touch_journal; fi
  exit "${{FAKE_PYTEST_EXIT:-0}}"
fi
if [ "$1" = run ] && [ "$2" = ruff ]; then exit "${{FAKE_RUFF_EXIT:-0}}"; fi
if [ "$1" = run ] && [ "$2" = python ]; then
  case "$*" in
    *open_journal*)
      if [ -n "${{FAKE_TOUCH_JOURNAL_MIGRATING:-}}" ]; then touch_journal; fi
      # A deploy that is killed mid-swap: say so, then stay here until the test signals us.
      if [ -n "${{FAKE_HANG_MIGRATION:-}}" ]; then : > "$SANDBOX_LOG.hanging"; sleep 4; fi
      exit "${{FAKE_MIGRATE_EXIT:-0}}" ;;
    *import\\ argus.cli*) exit "${{FAKE_SMOKE_EXIT:-0}}" ;;
  esac
fi
if [ "$1" = run ] && [ "$2" = argus ]; then exit "${{FAKE_HELP_EXIT:-0}}"; fi
if [ "$1" = sync ]; then
  if [ -n "${{FAKE_TOUCH_JOURNAL_SYNCING:-}}" ]; then touch_journal; fi
  if [ -n "${{FAKE_DELETE_SNAPSHOT:-}}" ]; then rm -f "$ARGUS_HOME"/backup/pre-deploy-*.db; fi
  exit "${{FAKE_SYNC_EXIT:-0}}"
fi
exit 0
"""

RECORDER = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$SANDBOX_LOG"
exit 1
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class Sandbox:
    root: Path
    checkout: Path
    home: Path
    bin: Path
    log: Path
    env: dict[str, str]
    origin: Path
    other: Path

    def push_main(self, files: dict[str, str], message: str) -> str:
        """A further release lands on origin/main from somewhere else; returns its sha."""
        for name, body in files.items():
            path = self.other / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.git("add", "-A", cwd=self.other)
        self.git("commit", "-q", "-m", message, cwd=self.other)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.other)
        return self.git("rev-parse", "HEAD", cwd=self.other)

    def promote(self, sha: str | None = None) -> str:
        """Point the scratch origin deployment ref at a commit."""
        sha = sha or self.git("rev-parse", "HEAD", cwd=self.other)
        self.git("push", "-q", "origin", f"{sha}:refs/heads/deploy/green", cwd=self.other)
        return sha

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.checkout,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def kill_9_during_the_migration(self, *args: str) -> None:
        """Start a deploy, wait until it is inside the swap (code merged, journal touched), then
        SIGKILL the whole process group — an OOM kill or a power cut. No trap runs."""
        hanging = self.root / "calls.log.hanging"
        env = {**self.env, "FAKE_HANG_MIGRATION": "1", "FAKE_TOUCH_JOURNAL_SYNCING": "1"}
        proc = subprocess.Popen(
            ["bash", str(self.checkout / "deploy" / "update.sh"), *args],
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while not hanging.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert hanging.exists(), "the swap never reached the migration"
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=30)
        finally:
            proc.kill()
        hanging.unlink()

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.checkout / "deploy" / script), *args],
            cwd=self.root,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def journal_rows(self) -> list[str]:
        with sqlite3.connect(self.home / "journal.db") as conn:
            return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY rowid")]

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


def build(
    tmp: Path,
    scripts: tuple[str, ...] = ("update.sh",),
    v2_touches: str = "",
    v1_from: dict[str, Path] | None = None,
    v2_from: dict[str, Path] | None = None,
) -> Sandbox:
    """A checkout one commit behind its origin, holding copies of the named deploy scripts.

    `v2_touches` names a deploy script the new release also changes (a trailing comment), which
    is what makes update.sh re-exec its newer self. `v1_from` / `v2_from` map a deploy script to a
    file to use instead of the repo's, for release 1 / added in release 2 — a transition from an
    OLDER update.sh to the current one, which is where a deploy first meets a new lock.
    """
    root = tmp / "box"
    bin_dir = root / "bin"
    home = root / "home"
    checkout = root / "checkout"
    origin = root / "origin.git"
    for d in (bin_dir, home):
        d.mkdir(parents=True)
    log = root / "calls.log"

    _write_exec(bin_dir / "uv", FAKE_UV)
    for tool in ("sudo", "systemctl"):
        _write_exec(bin_dir / tool, RECORDER)
    if shutil.which("flock") is None:
        _write_exec(bin_dir / "flock", FLOCK_SHIM)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(root),
        "ARGUS_HOME": str(home),
        "SANDBOX_LOG": str(log),
        # A sandbox git that ignores the developer's global config and hooks.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "sandbox",
        "GIT_AUTHOR_EMAIL": "sandbox@example.invalid",
        "GIT_COMMITTER_NAME": "sandbox",
        "GIT_COMMITTER_EMAIL": "sandbox@example.invalid",
    }
    box = Sandbox(root, checkout, home, bin_dir, log, env, origin, root / "other")

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=env, check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], env=env, check=True)
    (checkout / "deploy").mkdir()
    for name in scripts:
        shutil.copy2((v1_from or {}).get(name, REPO / "deploy" / name), checkout / "deploy" / name)
    (checkout / "app.txt").write_text("v1\n")
    (checkout / "uv.lock").write_text("lock v1\n")
    box.git("add", "-A")
    box.git("commit", "-q", "-m", "v1")
    box.git("push", "-q", "origin", "HEAD:main")
    v1 = box.git("rev-parse", "HEAD")

    # The next release lands on origin from somewhere else, as a push from the Mac would.
    other = root / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], env=env, check=True)
    (other / "app.txt").write_text("v2\n")
    for name, source in (v2_from or {}).items():
        shutil.copy2(source, other / "deploy" / name)
    if v2_touches:
        with (other / "deploy" / v2_touches).open("a") as fh:
            fh.write("# changed in v2\n")
    box.git("commit", "-q", "-am", "v2", cwd=other)
    box.git("push", "-q", "origin", "HEAD:main", cwd=other)
    assert box.git("rev-parse", "HEAD") == v1

    with sqlite3.connect(home / "journal.db") as conn:
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('before the deploy')")
    return box
