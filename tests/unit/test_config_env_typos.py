"""An env typo must not be dropped silently, and errors must not echo secrets (review S-5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import argus.doctor as doctor
from argus.config import Settings, unknown_env_keys


def _env(tmp_path: Path, text: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(text)
    return p


def test_typo_in_env_file_is_reported(tmp_path: Path) -> None:
    p = _env(tmp_path, "RISK_PROFLE=aggressive\nMODE=demo\n")
    assert unknown_env_keys(p) == {"RISK_PROFLE": "RISK_PROFILE"}


def test_unrelated_variables_are_not_flagged(tmp_path: Path) -> None:
    p = _env(tmp_path, "# comment\nPATH_EXTRA=/x\nAWS_REGION=eu\nexport OKX_API_KEY=abc\n")
    assert unknown_env_keys(p) == {}


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert unknown_env_keys(tmp_path / "nope") == {}


def test_validation_errors_do_not_echo_input() -> None:
    with pytest.raises(ValidationError) as ei:
        Settings(_env_file=None, okx_api_key=object(), mode="s3cr3t-value")  # type: ignore[arg-type]
    assert "s3cr3t-value" not in str(ei.value)


def test_doctor_warns_on_typo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _env(tmp_path, "RISK_PROFLE=aggressive\n")
    check = next(c for c in doctor.check_config() if c.name == ".env keys")
    assert not check.ok
    assert "RISK_PROFLE" in check.detail
