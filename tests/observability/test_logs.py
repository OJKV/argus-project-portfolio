"""Every run leaves a JSON log on disk, rotated, with its run_id (review O-4 / P-N9).

structlog was never configured: events went to stdout in the default dev format, so after an
incident the only record was whatever journald kept, as text, with no run_id to join it to the
journal.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from argus.observability import logs


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    logs.reset_for_tests()


def _flush() -> None:
    for handler in logging.getLogger("argus").handlers:
        handler.flush()


def test_events_land_as_json_lines_with_the_run_id(tmp_path: Path) -> None:
    path = logs.configure_logging(tmp_path, "argus-test")
    assert path is not None
    structlog.contextvars.bind_contextvars(run_id="r-123")
    try:
        structlog.get_logger("argus.journal.db").info("run.started", answer=42)
    finally:
        structlog.contextvars.unbind_contextvars("run_id")
    _flush()

    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    event = json.loads(line)
    assert event["event"] == "run.started"
    assert event["answer"] == 42
    assert event["run_id"] == "r-123"
    assert event["level"] == "info"
    assert event["service"] == "argus-test"
    assert event["timestamp"].startswith("20")


def test_the_file_rotates(tmp_path: Path) -> None:
    logs.configure_logging(tmp_path, "argus-test")
    rotating = [
        h
        for h in logging.getLogger("argus").handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0].maxBytes > 0 and rotating[0].backupCount > 0


def test_the_console_stays_human(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logs.configure_logging(tmp_path, "argus-test")
    structlog.get_logger("argus.x").warning("telegram.disabled", reason="no token")
    err = capsys.readouterr().err
    assert "telegram.disabled" in err
    assert not err.lstrip().startswith("{")


def test_third_party_request_logs_never_reach_the_file(tmp_path: Path) -> None:
    """httpx logs every request URL at INFO, and a Telegram URL contains the bot token (F-133)."""
    path = logs.configure_logging(tmp_path, "argus-test")
    assert path is not None
    logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot1:SECRET/x")
    _flush()
    assert "SECRET" not in path.read_text(encoding="utf-8")


def test_an_unwritable_home_degrades_to_console_only(tmp_path: Path) -> None:
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert logs.configure_logging(blocker, "argus-test") is None
    structlog.get_logger("argus.x").info("still.logs")  # must not raise


def test_every_scheduled_command_configures_logging(tmp_path: Path) -> None:
    """The wiring: `_telemetry` is what manager, review, watchdog and reflect call first."""
    import argus.cli as cli
    from argus.config import get_settings

    cli._telemetry("argus-test")
    _flush()
    assert (get_settings().argus_home / "logs" / logs.LOG_FILE).exists()
