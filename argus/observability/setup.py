"""Logfire wiring (OBSERVABILITY_AND_GUARDRAILS.md §1).

Two rules shape this module:

1. **Observability may never break a run.** No token, an unreachable Logfire, a bad instrument —
   all of it degrades to a no-op. A run that cannot report is still a run that must place its stop.
2. **Nothing secret leaves the box.** httpx instrumentation is configured without headers or
   bodies on purpose: OKX authenticates with `OK-ACCESS-KEY`/`OK-ACCESS-PASSPHRASE` request
   headers, so capturing them would ship live credentials to a third party (AGENTS.md rule 2).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog

from argus.config import get_settings

log = structlog.get_logger(__name__)

# Query parameters whose VALUE is a credential. Matched case-insensitively against the parameter
# name, so `api_key`, `API-KEY` and `x_cg_demo_api_key` are all covered.
SECRET_PARAM = re.compile(r"(key|token|secret|password|passphrase|auth|sig|signature)", re.I)
# A healthchecks.io ping URL *is* the secret; so is anything else shaped like a UUID in a path.
UUID_SEGMENT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Telegram carries the bot token in the URL path: /bot<id>:<token>/sendMessage
TELEGRAM_BOT = re.compile(r"^bot\d+:.+$")
REDACTED = "***"


def _scrub_segment(segment: str) -> str:
    if UUID_SEGMENT.match(segment):
        return REDACTED
    if TELEGRAM_BOT.match(segment):
        return f"bot{REDACTED}"
    return segment


def scrub_url(url: str) -> str:
    """Strip credentials out of a URL before it becomes a span attribute (review S-2).

    `capture_headers=False` says nothing about `url.full`, which OpenTelemetry records on every
    httpx span and which Logfire's scrubber treats as safe. Telegram puts the bot token in the
    path, FRED and Coinalyze put `api_key=` in the query, and a healthchecks.io ping URL is itself
    the secret. Anyone with read access to the Logfire project could otherwise impersonate the
    alert channel.

    Only the credential is replaced — host, path shape and harmless parameters survive, because a
    trace you cannot read is not worth exporting.
    """
    parts = urlsplit(url)
    segments = [
        REDACTED
        if UUID_SEGMENT.match(seg)
        else "bot" + REDACTED
        if TELEGRAM_BOT.match(seg)
        else seg
        for seg in parts.path.split("/")
    ]
    query = urlencode(
        [
            (name, REDACTED if SECRET_PARAM.search(name) else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), query, parts.fragment))


def _httpx_request_hook(span: Any, request: Any) -> None:
    """Overwrite the URL attributes OpenTelemetry has just recorded with scrubbed copies."""
    try:
        scrubbed = scrub_url(str(request.url))
        for attribute in ("url.full", "http.url"):
            span.set_attribute(attribute, scrubbed)
        span.set_attribute("url.query", scrub_url(f"http://x?{request.url.query.decode()}"))
    except Exception:  # noqa: BLE001 - a hook must never break the request it is observing
        span.set_attribute("url.full", REDACTED)


_state: dict[str, bool] = {
    "configured": False,
    "enabled": False,
    "httpx": False,
    "llm": False,
}


def enabled() -> bool:
    """True when spans and metrics are actually being exported."""
    return _state["enabled"]


def configure(service: str) -> bool:
    """Idempotent set-up for one process. Returns whether telemetry is live."""
    if _state["configured"]:
        return _state["enabled"]
    _state["configured"] = True
    settings = get_settings()
    token = settings.logfire_token.get_secret_value().strip()
    if not token:
        log.debug("observability.disabled", reason="no LOGFIRE_TOKEN")
        return False
    try:
        import logfire

        logfire.configure(
            token=token,
            service_name=service,
            environment=settings.mode,
            send_to_logfire="if-token-present",
            console=False,
        )
        # httpx instrumentation is optional and deliberately separate. The URL itself carries
        # the Telegram bot token and every `api_key=` query parameter (F-070/S-2), so it may only
        # be attached together with the scrubbing hook. If the hook cannot be installed we run
        # without httpx capture rather than without telemetry: no capture means no URLs, which is
        # safe, whereas the old code let this failure disable spans, metrics and alerts as well
        # and nobody noticed for a week (F-121).
        try:
            logfire.instrument_httpx(
                capture_headers=False,
                capture_request_body=False,
                request_hook=_httpx_request_hook,
            )
            _state["httpx"] = True
        except Exception as exc:  # noqa: BLE001 - telemetry never aborts a run
            log.warning(
                "observability.httpx_instrumentation_skipped",
                error=f"{type(exc).__name__}: {exc}",
                effect="spans and metrics still work; outbound request URLs are not captured",
            )
        # The LLM calls themselves: prompt, completion, model, tokens, latency, and the retry
        # chain when the router falls through providers. Without this Logfire saw our own spans
        # and nothing about the thing the system actually spends its time doing (F-128), while
        # pydantic-ai printed "observability: off" on every run — which read as a broken setup.
        #
        # Prompts carry dossier data: public market numbers and news headlines. No keys, no
        # account identifiers. The URL scrubbing that F-070 needed does not apply here.
        try:
            logfire.instrument_pydantic_ai()
            _state["llm"] = True
        except Exception as exc:  # noqa: BLE001 - telemetry never aborts a run
            log.warning(
                "observability.pydantic_ai_instrumentation_skipped",
                error=f"{type(exc).__name__}: {exc}",
            )

        _state["enabled"] = True
        log.info(
            "observability.enabled",
            service=service,
            environment=settings.mode,
            httpx=_state["httpx"],
            llm=_state["llm"],
        )
    except Exception as exc:  # noqa: BLE001 - telemetry never aborts a run
        log.warning("observability.configure_failed", error=f"{type(exc).__name__}: {exc}")
    return _state["enabled"]


def instrument_engine(engine: Any) -> None:
    """Attach SQLAlchemy spans to the journal engine (after `configure`)."""
    if not enabled():
        return
    try:
        import logfire

        logfire.instrument_sqlalchemy(engine=engine)
    except Exception as exc:  # noqa: BLE001
        log.warning("observability.sqlalchemy_failed", error=f"{type(exc).__name__}: {exc}")


def reset_for_tests() -> None:
    _state.update(configured=False, enabled=False, httpx=False, llm=False)
