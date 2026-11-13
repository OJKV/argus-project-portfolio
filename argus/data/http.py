"""Shared fetch path: rate limit → cache → retry with jitter → Observation (ARCHITECTURE.md §4).

Every fetcher goes through `fetch_json`, so provenance, caching and limits are impossible to
forget. Retries count **attempts** (F-029) and a failure returns a `missing` Observation rather
than raising — a dead source degrades the dossier, it does not kill the run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from argus.config import get_settings
from argus.data.cache import DEFAULT_TTL, TTL_SECONDS, cached
from argus.data.envelope import Observation, Tier
from argus.data.ratelimit import bucket_key, limiter
from argus.exchange.breakers import BreakerRegistry

log = structlog.get_logger(__name__)

USER_AGENT = "argus/0.1 (trading research; contact via repo)"
# Connect fast, read patiently: a geo-blocked host fails on connect, and waiting 20s for that
# five times over burns the 8-minute run budget (measured 2026-09-16 with OKX blocked).
TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
RETRYABLE = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

# One breaker per host. After two failures a blocked host is skipped outright for the rest of
# the run instead of being retried by every fetcher that happens to use it. An endpoint family
# with its own rate limit (`PATH_LIMITS`) also has its own breaker: two rubik 429s opened the
# www.okx.com breaker and skipped candles and funding, the trade-critical families.
host_breakers = BreakerRegistry(threshold=2, window_s=300.0, cooldown_s=60.0)
# Statuses that describe *this request* (unknown slug, bad params, paid-only endpoint), not the
# host. Two of them must not open the breaker for every other asset on that host (D-18f).
# 401/403 stay host failures: geo-blocks and Cloudflare walls answer that way (F-034, F-055).
REQUEST_ERRORS = frozenset({400, 402, 404, 405, 410, 422})
# No legitimate source sends more; a provider that does must not hold the run's memory.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class HttpError(RuntimeError):
    pass


class PayloadError(HttpError):
    """The host answered, but not with usable data for this request (an error body, a 404, an
    oversized response). Never cached, and not a host outage, so it does not trip the breaker."""


class SuspectPayloadError(PayloadError):
    """The host answered, but the caller's validator rejects the body (an empty export, a block
    page). Never cached; returned as a `suspect` Observation with the body attached."""

    def __init__(self, problem: str, payload: Any) -> None:
        super().__init__(problem)
        self.payload = payload


def okx_problem(payload: Any) -> str | None:
    """OKX answers HTTP 200 with `code != "0"` for business errors (bad instId, rate limit,
    region block). Without this check that body was cached as quality "ok" (D-9)."""
    if not isinstance(payload, dict) or "code" not in payload:
        return "OKX response has no `code` field"
    if str(payload["code"]) != "0":
        return f"OKX code {payload['code']}: {str(payload.get('msg') or '')[:120]}"
    return None


def _is_okx(host: str) -> bool:
    return host == "okx.com" or host.endswith(".okx.com")


def _validated(payload: Any, host: str) -> Any:
    problem = okx_problem(payload) if _is_okx(host) else None
    if problem is not None:
        raise PayloadError(problem)
    return payload


def build_transport(force_ipv4: bool) -> httpx.HTTPTransport | None:
    """Bind outbound sockets to IPv4 when the exchange allowlist is IPv4-only (F-060)."""
    return httpx.HTTPTransport(local_address="0.0.0.0") if force_ipv4 else None


@retry(
    retry=retry_if_exception_type(RETRYABLE),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=4),
    reraise=True,
)
def _request(
    method: str,
    url: str,
    params: dict[str, Any] | None,
    json_body: dict[str, Any] | None,
    headers: dict[str, str] | None,
    as_text: bool = False,
) -> Any:
    limiter.acquire(url)
    merged = {"User-Agent": USER_AGENT, **(headers or {})}
    transport = build_transport(get_settings().force_ipv4)
    with (
        httpx.Client(timeout=TIMEOUT, follow_redirects=True, transport=transport) as client,
        client.stream(method, url, params=params, json=json_body, headers=merged) as response,
    ):
        body = _read_capped(response, url)
        text = body.decode(response.encoding or "utf-8", errors="replace")
    status = response.status_code
    if status == 402:
        raise PayloadError(f"HTTP 402 from {url}: endpoint is paid-only")
    if status >= 400:
        error = PayloadError if status in REQUEST_ERRORS else HttpError
        raise error(f"HTTP {status} from {url}: {text[:200]}")
    return text if as_text else json.loads(body)


def _read_capped(response: httpx.Response, url: str) -> bytes:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise PayloadError(f"response from {url} exceeds {MAX_RESPONSE_BYTES} bytes")
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise PayloadError(f"response from {url} exceeds {MAX_RESPONSE_BYTES} bytes")
    return bytes(body)


def fetch_json(
    source: str,
    url: str,
    tier: Tier,
    family: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    asof: datetime | None = None,
    force: bool = False,
    as_text: bool = False,
    validate: Callable[[Any], str | None] | None = None,
) -> Observation:
    """Fetch, cache and wrap. Never raises for an upstream failure.

    `as_text=True` returns the body as `{"text": ...}` (RSS, XML) instead of parsed JSON.
    `validate(payload)` returns why a body is unusable, or None. It runs inside the cache
    producer, so a rejected body comes back `suspect` and is never cached; checked after the
    cache, an empty calendar export held the regime at CHOP for the whole 6 h window. A cached
    body it rejects now (a weekly export across its rollover) is fetched again once.
    """
    host = urlparse(url).netloc
    dependency = bucket_key(url)
    breaker = host_breakers.get(dependency)
    key_params = {"url": url, "params": params or {}, "body": json_body or {}, "text": as_text}
    if not breaker.allow():
        log.info("fetch.skipped", source=source, host=dependency, reason="breaker open")
        return Observation.missing(source, tier, f"{dependency} breaker open — skipped this run")

    def produce() -> Any:
        # Validated inside the producer so an error body is never written to the cache.
        payload = _validated(_request(method, url, params, json_body, headers, as_text), host)
        problem = validate(payload) if validate is not None else None
        if problem is not None:
            raise SuspectPayloadError(problem, payload)
        return payload

    try:
        payload, was_cached, fetched_at = cached(source, key_params, family, produce, force=force)
        if was_cached and validate is not None and validate(payload) is not None:
            payload, was_cached, fetched_at = cached(
                source, key_params, family, produce, force=True
            )
    except SuspectPayloadError as exc:
        log.warning("fetch.suspect", source=source, problem=str(exc))
        return Observation(
            source=source,
            tier=tier,
            asof=asof,
            quality="suspect",
            notes=[str(exc)],
            data=_envelope_data(exc.payload, as_text),
        )
    except Exception as exc:  # noqa: BLE001 - a dead source degrades, never kills the run
        if not isinstance(exc, PayloadError):
            breaker.record_failure()
        log.warning("fetch.failed", source=source, error=f"{type(exc).__name__}: {exc}")
        return Observation.missing(source, tier, f"fetch failed: {type(exc).__name__}: {exc}")
    if not was_cached:
        # A cache hit says nothing about the host; counting it as a success cleared the failure
        # window, so a blocked host interleaved with cached reads never tripped (D-18f).
        breaker.record_success()
    notes = ["from cache"] if was_cached else []
    return Observation(
        source=source,
        tier=tier,
        observed_at=fetched_at,
        asof=asof,
        quality="ok",
        notes=notes,
        data=_envelope_data(payload, as_text),
    )


def _envelope_data(payload: Any, as_text: bool) -> dict[str, Any] | list[Any]:
    """The envelope holds dicts or lists; scalars (DefiLlama `/tvl` returns a bare number) and
    text bodies are wrapped so a valid response can never crash the run."""
    if as_text:
        return {"text": payload}
    if isinstance(payload, dict | list):
        return payload
    return {"value": payload}


def with_venue_asof(obs: Observation, asof: datetime | None, family: str) -> Observation:
    """Stamp the venue's own data time and mark the observation `stale` when it is older than the
    family's cache TTL (the one freshness bound already defined; Muse P1-2).

    Age is taken against `observed_at`, when we fetched: a cache hit repeats the same venue lag,
    so it is not aged again here (the TTL already bounds how long a body is reused). No timestamp
    leaves the observation unstamped and `ok`.
    """
    if asof is None:
        return obs
    ttl = timedelta(seconds=TTL_SECONDS.get(family, DEFAULT_TTL))
    return obs.model_copy(update={"asof": asof}).mark_stale(ttl, now=obs.observed_at)


def iso_to_utc(value: Any) -> datetime | None:
    """An ISO-8601 string (CoinGecko `last_updated`) as UTC, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def ms_to_utc(value: Any) -> datetime | None:
    """OKX/Binance/Bybit all stamp in epoch milliseconds."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


def s_to_utc(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError):
        return None
