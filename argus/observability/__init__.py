"""Telemetry: Logfire spans, metrics and alert rules.

Import order matters a little — `setup.configure()` must run before the first span, which the CLI
does at the start of every command. Everything degrades to a no-op when Logfire is unconfigured.
"""

from argus.observability.alerts import Alert, dispatch
from argus.observability.metrics import count, gauge, observe
from argus.observability.setup import configure, enabled, instrument_engine
from argus.observability.spans import span

__all__ = [
    "Alert",
    "configure",
    "count",
    "dispatch",
    "enabled",
    "gauge",
    "instrument_engine",
    "observe",
    "span",
]
