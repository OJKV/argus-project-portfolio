"""Read the shipped systemd units without a systemd. Shared by the deploy tests."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
SYSTEMD = DEPLOY / "systemd"


def unit_values(name: str, key: str) -> list[str]:
    """Every value of `key` in a unit file. systemd allows repeated keys, configparser does not."""
    values = []
    for raw in (SYSTEMD / name).read_text().splitlines():
        line = raw.strip()
        if line.startswith(f"{key}="):
            values.append(line.split("=", 1)[1].strip())
    return values
