"""A failed manager, review or reflection run reaches Telegram (review 2026-10-07, J-N12, J-N3).

The units had no `OnFailure=` and no healthcheck of their own (HEALTHCHECKS_URL is the
watchdog's heartbeat, F-126). A review that exited 1 because OKX rejected a CLOSE left its trace
in journald and nowhere else; a manager that crashed before its first plan was silent until the
14-hour staleness alert.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from argus.notify import telegram
from argus.notify.telegram import Notifier, RecordingBot

from .systemd_units import DEPLOY, unit_values

TEMPLATE = "argus-failure@.service"
ALERTED = [
    "argus-manager.service",
    "argus-review.service",
    "argus-reflect.service",
    "argus-live-keys.service",
]


@pytest.mark.parametrize("service", ALERTED)
def test_a_failure_starts_the_alert_unit(service: str) -> None:
    assert unit_values(service, "OnFailure") == ["argus-failure@%n.service"]


def test_the_alert_unit_runs_as_argus_and_names_the_failed_unit() -> None:
    assert unit_values(TEMPLATE, "User") == ["argus"]
    (exec_start,) = unit_values(TEMPLATE, "ExecStart")
    assert exec_start.endswith("/opt/argus-project/deploy/unit_failed.py %i")


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("unit_failed", DEPLOY / "unit_failed.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_alert_names_the_unit_and_where_to_look(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = RecordingBot()
    monkeypatch.setattr(
        telegram.Notifier, "from_settings", classmethod(lambda cls: Notifier(bot, "42"))
    )

    assert _script().main(["unit_failed.py", "argus-review.service"]) == 0

    ((_, text),) = bot.sent
    assert "argus-review.service" in text
    assert "journalctl -u argus-review.service" in text


def test_the_alert_never_carries_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram parses these as HTML; a stray `<` drops the whole message (O-1)."""
    bot = RecordingBot()
    monkeypatch.setattr(
        telegram.Notifier, "from_settings", classmethod(lambda cls: Notifier(bot, "42"))
    )
    _script().main(["unit_failed.py", "argus-<b>x</b>.service"])
    ((_, text),) = bot.sent
    assert "<" not in text and ">" not in text


def test_an_unconfigured_telegram_fails_the_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit non-zero, so `systemctl --failed` shows the alert itself did not go out."""
    monkeypatch.setattr(
        telegram.Notifier, "from_settings", classmethod(lambda cls: Notifier(None, ""))
    )
    assert _script().main(["unit_failed.py", "argus-review.service"]) == 1


def test_the_review_unit_says_it_is_armed() -> None:
    """J-N12: CLAUDE.md documents `argus review --dry-run`; the unit runs it armed."""
    (exec_start,) = unit_values("argus-review.service", "ExecStart")
    assert "--dry-run" not in exec_start
    text = (DEPLOY / "systemd" / "argus-review.service").read_text()
    assert "ARMED" in text
    claude = " ".join((Path(DEPLOY).parent / "CLAUDE.md").read_text().split())
    assert "argus-review.service runs `argus review` armed" in claude
