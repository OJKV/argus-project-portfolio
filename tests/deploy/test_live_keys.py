"""The weekly live-key check: under the run lock, without .env in its environment, and it says
whether the key is IP-bound (review 2026-10-07, P-N7; F-037).

`argus-live-keys.service` woke the LIVE key every Monday with the whole `.env` exported into its
environment and no lock, so it could hit OKX's rate limits alongside the :00 watchdog tick. The
IP-binding warning F-037 promised did not exist: OKX deletes a trade key that is not bound to an
IP after 14 idle days, and nothing would have said the binding had been lost.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from argus import config, doctor
from argus.runs.lock import LOCK_NAME

from .systemd_units import unit_values

UNIT = "argus-live-keys.service"


def test_the_live_key_check_takes_the_run_lock() -> None:
    (exec_start,) = unit_values(UNIT, "ExecStart")
    argv = exec_start.split()
    assert argv[0] == "/usr/bin/flock"
    assert f"/var/lib/argus/{LOCK_NAME}" in argv
    assert argv[-2:] == ["doctor", "--live-keys"]


def test_the_live_key_check_does_not_export_dotenv_into_its_environment() -> None:
    """argus reads .env itself from WorkingDirectory; the environment needs none of it."""
    assert unit_values(UNIT, "EnvironmentFile") == []
    assert unit_values(UNIT, "WorkingDirectory") == ["/opt/argus-project"]


@pytest.mark.parametrize("ips", ["", None])
def test_an_unbound_key_fails_the_check(ips: str | None) -> None:
    check = doctor.ip_binding_check("live", ips)
    assert not check.ok
    assert "14 days" in check.detail


def test_a_bound_key_passes_without_printing_the_address() -> None:
    check = doctor.ip_binding_check("live", "203.0.113.7,2001:db8::7")
    assert check.ok
    assert "2 address" in check.detail
    assert "203.0.113.7" not in check.detail


class _FakeOkx:
    def __init__(self, _cfg: dict[str, Any]) -> None:
        pass

    def set_sandbox_mode(self, _on: bool) -> None:
        pass

    def private_get_account_config(self) -> dict[str, Any]:
        row = {"acctLv": "2", "posMode": "net_mode", "perm": "read_only,trade", "ip": ""}
        return {"data": [row]}

    def private_get_account_balance(self) -> dict[str, Any]:
        raise RuntimeError("not needed here")

    def private_get_account_trade_fee(self, _params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("not needed here")


def test_doctor_auth_reports_the_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("KEY", "SECRET", "PASSPHRASE"):
        monkeypatch.setenv(f"OKX_DEMO_API_{name}", "x")
    config.get_settings.cache_clear()
    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(okx=_FakeOkx))

    rows = {c.name: c for c in doctor.check_okx_auth(env="demo")}

    assert "okx.demo.ip binding" in rows
    assert not rows["okx.demo.ip binding"].ok
