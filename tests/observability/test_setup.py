"""Telemetry must degrade to nothing: no token, no spans, no exceptions, no behaviour change."""

from __future__ import annotations

import pytest

from argus.observability import metrics, setup, spans


@pytest.fixture(autouse=True)
def _clean_state():
    setup.reset_for_tests()
    metrics.reset_for_tests()
    yield
    setup.reset_for_tests()
    metrics.reset_for_tests()


def test_configure_without_a_token_is_a_no_op() -> None:
    assert setup.configure("argus-test") is False
    assert setup.enabled() is False


def test_configure_is_idempotent() -> None:
    assert setup.configure("argus-test") is setup.configure("argus-test")


def test_span_runs_the_body_when_disabled() -> None:
    seen = []
    with spans.span("manager.run", run_id="r1"):
        seen.append(1)
    assert seen == [1]


def test_span_does_not_swallow_errors() -> None:
    with pytest.raises(ValueError), spans.span("manager.run"):
        raise ValueError("boom")


def test_metrics_are_silent_when_disabled() -> None:
    metrics.count(metrics.GATE_REJECTIONS, reason="heat")
    metrics.gauge(metrics.POSITIONS_UNPROTECTED, 0)
    metrics.observe(metrics.RUN_DURATION, 71.0, unit="s")
    assert metrics._instruments == {}


def test_instrument_engine_without_telemetry_does_nothing() -> None:
    setup.instrument_engine(object())


def _token_settings():  # noqa: ANN202 - a stub settings object, shape is all that matters
    from pydantic import SecretStr

    class _S:
        logfire_token = SecretStr("tok")
        mode = "demo"

    return _S()


def test_telemetry_survives_a_missing_httpx_instrumentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing scrubber must cost URL capture, not all observability.

    The old code called `instrument_httpx()` inline, so when the optional
    `opentelemetry-instrumentation-httpx` package was absent the exception jumped past
    `_state["enabled"] = True`. Spans, metrics and alerts were silently off on both machines and
    the only clue was one warning line nobody read (F-121).
    """
    import logfire

    monkeypatch.setattr(setup, "get_settings", _token_settings)
    monkeypatch.setattr(logfire, "configure", lambda **_k: None)

    def boom(**_k: object) -> None:
        raise RuntimeError("requires the `opentelemetry-instrumentation-httpx` package")

    monkeypatch.setattr(logfire, "instrument_httpx", boom)
    assert setup.configure("test") is True
    assert setup.enabled() is True
    # No capture means no URLs reach Logfire at all, which is the safe direction to fail in.
    assert setup._state["httpx"] is False


def test_httpx_capture_is_only_enabled_with_the_scrubbing_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logfire

    seen: dict[str, object] = {}
    monkeypatch.setattr(setup, "get_settings", _token_settings)
    monkeypatch.setattr(logfire, "configure", lambda **_k: None)
    monkeypatch.setattr(logfire, "instrument_httpx", lambda **k: seen.update(k))
    assert setup.configure("test") is True
    # The URL itself carries the Telegram bot token and every api_key= parameter (F-070).
    assert seen["request_hook"] is setup._httpx_request_hook
    assert seen["capture_headers"] is False
    assert seen["capture_request_body"] is False
    assert setup._state["httpx"] is True


def test_llm_calls_are_instrumented(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LLM calls are the thing this system spends its time doing.

    Without `instrument_pydantic_ai()` Logfire saw our own spans and nothing about the prompts,
    completions, token counts or the router's fallback chain — and pydantic-ai printed
    "observability: off" on every run, which read as a broken setup (F-128).
    """
    import logfire

    called: list[str] = []
    monkeypatch.setattr(setup, "get_settings", _token_settings)
    monkeypatch.setattr(logfire, "configure", lambda **_k: None)
    monkeypatch.setattr(logfire, "instrument_httpx", lambda **_k: None)
    monkeypatch.setattr(logfire, "instrument_pydantic_ai", lambda: called.append("llm"))

    assert setup.configure("test") is True
    assert called == ["llm"]
    assert setup._state["llm"] is True


def test_a_missing_llm_instrumentation_still_leaves_telemetry_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logfire

    monkeypatch.setattr(setup, "get_settings", _token_settings)
    monkeypatch.setattr(logfire, "configure", lambda **_k: None)
    monkeypatch.setattr(logfire, "instrument_httpx", lambda **_k: None)

    def boom() -> None:
        raise RuntimeError("no pydantic-ai integration")

    monkeypatch.setattr(logfire, "instrument_pydantic_ai", boom)
    assert setup.configure("test") is True
    assert setup._state["llm"] is False
