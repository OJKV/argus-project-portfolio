"""`argus audit` is reachable through the sudoers-allowed checks script (review J-3 follow-up).

The audit existed but nothing could run it on the box: it was not in `checks.sh`, the one path the
operator account may run as `argus`. A check nobody can run verifies nothing.
"""

from __future__ import annotations

from pathlib import Path

CHECKS = Path(__file__).resolve().parents[2] / "deploy" / "checks.sh"


def test_checks_sh_runs_the_ledger_audit() -> None:
    lines = CHECKS.read_text(encoding="utf-8").splitlines()
    arm = [line.strip() for line in lines if line.strip().startswith("audit)")]
    assert arm == ['audit)    exec "$BIN" audit ;;'], arm
    usage = next(line for line in lines if "usage: checks.sh" in line)
    assert "|audit|" in usage
