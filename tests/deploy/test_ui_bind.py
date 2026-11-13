"""The dashboard binds loopback or not at all (review 2026-09-18 P-14; 2026-10-07 P-N10).

Nothing authenticates the journal behind the page, so the bind address is the whole access
control. `argus ui --host 0.0.0.0` printed a yellow warning and served the journal to the
internet anyway. Reaching it from owner's devices is `tailscale serve`'s job, which proxies to
127.0.0.1 and needs no other bind (F-134).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import argus.cli as cli

from .systemd_units import unit_values


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("argus.ui.app.serve", lambda port, host: calls.append(host))
    return calls


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "100.111.0.1", "192.168.1.5", "example.com"])
def test_a_non_loopback_bind_is_refused(host: str, served: list[str]) -> None:
    result = CliRunner().invoke(cli.app, ["ui", "--host", host])
    assert result.exit_code == 2, result.output
    assert served == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "auto"])
def test_loopback_binds_are_served(host: str, served: list[str]) -> None:
    result = CliRunner().invoke(cli.app, ["ui", "--host", host])
    assert result.exit_code == 0, result.output
    assert len(served) == 1


def test_the_unit_binds_loopback() -> None:
    (exec_start,) = unit_values("argus-ui.service", "ExecStart")
    assert "--host 127.0.0.1" in exec_start
