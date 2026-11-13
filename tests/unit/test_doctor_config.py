"""`argus doctor` judges risk_profiles.yaml the way a manager run does (review G-N5)."""

from __future__ import annotations

import copy

import pytest

import argus.doctor as doctor
from argus.config import load_yaml


def _risk_check() -> doctor.Check:
    return next(c for c in doctor.check_config() if c.name == "risk_profiles.yaml")


def test_doctor_passes_the_shipped_risk_file() -> None:
    assert _risk_check().ok


def test_doctor_fails_a_risk_file_every_manager_run_would_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor only checked that two keys existed and said "loaded"; the manager parses strictly
    and closes FAILED. An impossible risk_pct must fail here first, with the reason."""
    broken = copy.deepcopy(load_yaml("risk_profiles.yaml"))
    broken["small_account_steps"][1]["risk_pct"] = 35
    monkeypatch.setattr(doctor, "risk_config", lambda: broken)

    check = _risk_check()
    assert not check.ok
    assert "risk_pct" in check.detail
