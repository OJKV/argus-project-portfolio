"""Process logging: JSON lines to a rotating file, human lines on the console (review O-4/P-N9).

CLAUDE.md's convention — "structlog, JSON to file, human to console; include run_id" — was never
implemented: nothing called `structlog.configure`, so events went to stdout in the dev format and
the only post-incident record was journald's text. This is the one place it is set up, called
first by every scheduled command (`cli._telemetry`).

Only loggers under `argus` are routed here. httpx logs every request URL at INFO, and a Telegram
URL carries the bot token as a path segment (F-133), so third-party loggers keep their default of
warnings-only to stderr and never reach the file.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

LOG_DIR = "logs"
LOG_FILE = "argus.jsonl"
MAX_BYTES = 10 * 1024 * 1024  # ten files of history at most: 1 current + BACKUPS
BACKUPS = 9

_OWNED = "argus"


class _CurrentStderr(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever `sys.stderr` is at emit time, not the stream captured at set-up."""

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:
        pass


def configure_logging(home: Path, service: str) -> Path | None:
    """Route `argus.*` structlog events to the console and to `home/logs/argus.jsonl`.

    Returns the file path, or None when the file cannot be opened — then the console still works.
    Logging set-up, like telemetry, never stops a run. Safe to call more than once.
    """
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,  # run_id, bound by `start_run`
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service(service),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    owned = logging.getLogger(_OWNED)
    _drop_handlers(owned)
    owned.setLevel(logging.INFO)
    owned.propagate = False

    console = _CurrentStderr()
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=False),
            ],
            foreign_pre_chain=shared,
        )
    )
    owned.addHandler(console)

    path = home / LOG_DIR / LOG_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        structlog.get_logger(__name__).warning(
            "logging.file_unavailable", path=str(path), error=f"{type(exc).__name__}: {exc}"
        )
        return None
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(default=str),
            ],
            foreign_pre_chain=shared,
        )
    )
    owned.addHandler(file_handler)
    return path


def _add_service(service: str) -> Any:
    def add(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
        event.setdefault("service", service)
        return event

    return add


def _drop_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def reset_for_tests() -> None:
    _drop_handlers(logging.getLogger(_OWNED))
    logging.getLogger(_OWNED).propagate = True
    structlog.reset_defaults()
