"""`argus doctor --models` helpers: entry parsing and error classification (no network)."""

from __future__ import annotations

from pydantic_ai.exceptions import ModelHTTPError

from argus.doctor import _classify, _iter_model_entries


def test_entries_parse_provider_and_model_with_slashes() -> None:
    # Model ids contain slashes themselves, so only the FIRST segment is the provider. Asserted
    # as a property, not against literal ids: pinning ids is what let the catalogue rot unnoticed
    # until every free model 404'd on 2026-09-19.
    entries = _iter_model_entries()
    assert ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast") in entries
    nested = [(p, m) for p, m in entries if "/" in m]
    assert nested, "at least one entry must exercise a model id containing a slash"
    assert all(not p.endswith("/") and m for p, m in entries)


def test_entries_are_deduplicated() -> None:
    entries = _iter_model_entries()
    assert len(entries) == len(set(entries))


def test_classify_http_statuses() -> None:
    assert _classify(ModelHTTPError(429, "m")) == "429 rate-limited"
    assert _classify(ModelHTTPError(401, "m")) == "auth error"
    assert _classify(ModelHTTPError(403, "m")) == "auth error"
    assert _classify(ModelHTTPError(404, "m")) == "model-not-found"
    assert _classify(ModelHTTPError(500, "m", body="boom")).startswith("HTTP 500")


def test_classify_other_exception_is_named_and_truncated() -> None:
    detail = _classify(TimeoutError("x" * 500))
    assert detail.startswith("TimeoutError: ")
    assert len(detail) < 120


def test_a_busy_free_tier_is_not_a_broken_model() -> None:
    """429 and 503 are the normal weather on free tiers, and the router already handles them.

    Reporting them as failures made a healthy box exit non-zero and filled the screen with red
    rows for providers that were merely busy. Auth errors and missing models still fail.
    """
    from pydantic_ai.exceptions import ModelHTTPError

    from argus import doctor

    assert doctor._classify(ModelHTTPError(status_code=429, model_name="m")).startswith("429")
    assert doctor._classify(ModelHTTPError(status_code=503, model_name="m")).startswith("HTTP 503")
    assert doctor._classify(ModelHTTPError(status_code=401, model_name="m")) == "auth error"
    assert doctor._classify(ModelHTTPError(status_code=404, model_name="m")) == "model-not-found"
