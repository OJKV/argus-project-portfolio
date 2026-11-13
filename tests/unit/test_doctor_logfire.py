"""`argus doctor` names the Logfire project the token writes to, and never shows the token."""

from __future__ import annotations

import httpx
import pytest
import respx
from typer.testing import CliRunner

import argus.doctor as doctor
from argus.cli import app
from argus.config import Settings

US_TOKEN = "pylf_v1_us_SECRETSECRETSECRET"
EU_TOKEN = "pylf_v1_eu_SECRETSECRETSECRET"
INFO = {"project_url": "https://logfire-eu.pydantic.dev/me/argus", "project_name": "argus"}


def _use_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    settings = Settings(_env_file=None, logfire_token=token)
    monkeypatch.setattr(doctor, "get_settings", lambda: settings)


@respx.mock
def test_a_valid_token_shows_the_project_url_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, EU_TOKEN)
    route = respx.get("https://logfire-eu.pydantic.dev/v1/info").mock(
        return_value=httpx.Response(200, json=INFO)
    )
    (check,) = doctor.check_logfire()
    assert check.ok
    assert INFO["project_url"] in check.detail and "argus" in check.detail
    assert route.calls.last.request.headers["Authorization"] == EU_TOKEN
    assert EU_TOKEN not in check.detail


@respx.mock
def test_a_token_without_eu_uses_the_us_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, US_TOKEN)
    route = respx.get("https://logfire-us.pydantic.dev/v1/info").mock(
        return_value=httpx.Response(200, json={"project_url": "https://x/y"})
    )
    (check,) = doctor.check_logfire()
    assert route.called and check.ok and check.detail == "https://x/y"


@respx.mock
def test_a_rejected_token_fails_with_the_status_and_never_prints_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_token(monkeypatch, EU_TOKEN)
    respx.get("https://logfire-eu.pydantic.dev/v1/info").mock(
        return_value=httpx.Response(401, text=f"bad token {EU_TOKEN}")
    )
    (check,) = doctor.check_logfire()
    assert not check.ok
    assert check.detail == "HTTP 401"
    assert EU_TOKEN not in f"{check.name} {check.detail}"


@respx.mock
def test_a_network_error_fails_without_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, EU_TOKEN)
    respx.get("https://logfire-eu.pydantic.dev/v1/info").mock(side_effect=httpx.ConnectError("x"))
    (check,) = doctor.check_logfire()
    assert not check.ok and check.detail == "ConnectError"


@respx.mock
def test_no_token_means_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "")
    assert doctor.check_logfire() == []
    assert not respx.calls


def test_no_network_skips_the_logfire_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[doctor.Check]:
        raise AssertionError("--no-network must not call Logfire")

    monkeypatch.setattr(doctor, "check_logfire", boom)
    result = CliRunner().invoke(app, ["doctor", "--no-network"])
    assert not isinstance(result.exception, AssertionError)


def test_the_cli_prints_the_project_and_not_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "check_public", lambda: [])
    monkeypatch.setattr(
        doctor,
        "check_logfire",
        lambda: [doctor.Check("Logfire project", True, INFO["project_url"])],
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert INFO["project_url"] in result.output.replace("\n", "")
    assert "SECRET" not in result.output
