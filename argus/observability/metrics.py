"""Metric instruments (OBSERVABILITY_AND_GUARDRAILS.md §1).

Instruments are created lazily and cached: a metric name is cheap to reference from anywhere,
and when Logfire is not configured every call is a no-op. Names are the alert contract — the
Logfire alerts in §1 query these, so renaming one means editing the alert too.
"""

from __future__ import annotations

from typing import Any

import structlog

from argus.observability import setup

log = structlog.get_logger(__name__)

RUN_DURATION = "argus.run.duration_seconds"
GATE_REJECTIONS = "argus.gate.rejections"
POSITIONS_UNPROTECTED = "argus.positions.unprotected"
LLM_CALLS = "argus.llm.calls"
BREAKER_OPEN = "argus.exchange.breaker_open"
DATA_GAPS = "argus.data.gaps"

_instruments: dict[str, Any] = {}


def _instrument(kind: str, name: str, unit: str) -> Any:
    key = f"{kind}:{name}"
    made = _instruments.get(key)
    if made is None:
        import logfire

        factory = {
            "counter": logfire.metric_counter,
            "gauge": logfire.metric_gauge,
            "histogram": logfire.metric_histogram,
        }[kind]
        made = factory(name, unit=unit)
        _instruments[key] = made
    return made


def count(name: str, value: int = 1, unit: str = "", **labels: Any) -> None:
    if not setup.enabled():
        return
    try:
        _instrument("counter", name, unit).add(value, labels or None)
    except Exception as exc:  # noqa: BLE001 - a metric is never worth an exception
        log.debug("metrics.failed", metric=name, error=str(exc))


def gauge(name: str, value: float, unit: str = "", **labels: Any) -> None:
    if not setup.enabled():
        return
    try:
        _instrument("gauge", name, unit).set(value, labels or None)
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.failed", metric=name, error=str(exc))


def observe(name: str, value: float, unit: str = "", **labels: Any) -> None:
    if not setup.enabled():
        return
    try:
        _instrument("histogram", name, unit).record(value, labels or None)
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.failed", metric=name, error=str(exc))


def reset_for_tests() -> None:
    _instruments.clear()
