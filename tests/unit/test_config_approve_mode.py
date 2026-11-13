"""MODE=approve says out loud that it is MODE=auto (review 2026-10-07, P-N2).

There is no approval step: no QUEUED row is ever written, no `/approve`, and `place.py` places on
the gate's approval alone. An operator who set `approve` believed trades wait for a yes; they do
not. Until the step exists, every place an armed run announces itself has to say so.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import argus.cli as cli
from argus import config, doctor
from argus.config import APPROVE_IS_AUTO

ROOT = Path(__file__).resolve().parents[2]


def _approve(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(mode="approve", venue="demo", is_live=True, argus_home=tmp_path)


def _stop(*_a: object, **_k: object) -> None:
    raise SystemExit(0)


def test_the_armed_manager_banner_says_approve_is_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _approve(tmp_path))
    monkeypatch.setattr("argus.journal.db.open_journal", _stop)
    result = CliRunner().invoke(cli.app, ["manager", "--llm"])
    assert "ARMED" in result.output
    assert APPROVE_IS_AUTO in result.output.replace("\n", " ")


def test_the_armed_review_banner_says_approve_is_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _approve(tmp_path))
    monkeypatch.setattr("argus.journal.db.open_journal", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", _stop)
    result = CliRunner().invoke(cli.app, ["review"])
    assert "ARMED" in result.output
    assert APPROVE_IS_AUTO in result.output.replace("\n", " ")


def test_an_auto_banner_carries_no_such_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _approve(tmp_path)
    settings.mode = "auto"
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("argus.journal.db.open_journal", _stop)
    result = CliRunner().invoke(cli.app, ["manager", "--llm"])
    assert "ARMED" in result.output
    assert APPROVE_IS_AUTO not in result.output.replace("\n", " ")


def test_doctor_says_approve_is_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "approve")
    monkeypatch.setenv("LIVE_TRADING", "I_UNDERSTAND")
    config.get_settings.cache_clear()
    (row,) = [c for c in doctor.check_config() if c.name == "mode"]
    assert "approve == auto" in row.detail


@pytest.mark.parametrize("doc", [".env.example", "CLAUDE.md", "docs/SAFETY.md"])
def test_the_operator_docs_say_approve_is_auto(doc: str) -> None:
    text = " ".join((ROOT / doc).read_text().replace("`", "").split())
    assert "MODE=approve" in text and "exactly like MODE=auto" in text, doc
