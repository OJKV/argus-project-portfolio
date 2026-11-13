"""Executing a call: try, validate, repair once, fall to the next model, journal everything.

Offline — every model here is a pydantic-ai test double. What is being tested is the router's
behaviour around the call, which is where the money and the evidence trail are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.test import TestModel
from sqlalchemy import Engine, text

from argus.config import Settings
from argus.exchange.breakers import BreakerRegistry
from argus.journal.db import open_journal
from argus.llm.router import AllModelsFailedError, ModelRef, Router


class Verdict(BaseModel):
    side: str
    score: int


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_journal(tmp_path / "journal.db")
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES ('r1', 'manager', 'demo', '2026-09-20T00:00:00+00:00', 'RUNNING', 1, '-')"
            )
        )
    return eng


def _router(engine: Engine, factory: Any) -> Router:
    settings = Settings(
        _env_file=None,
        gemini_api_key="g",
        groq_api_key="g",
        openrouter_api_key="g",
        mistral_api_key="g",
        cloudflare_api_token="g",
        cloudflare_account_id="acct",
        zhipu_api_key="g",
    )
    return Router(
        engine=engine, settings=settings, breakers=BreakerRegistry(), model_factory=factory
    )


def _rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT * FROM llm_calls ORDER BY created_at, attempt")
            ).mappings()
        ]


def _ok_model(ref: ModelRef) -> Any:
    """Answers validly whichever way it is asked: Groq gets prompted JSON, the rest a tool call."""
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not info.output_tools:
            return ModelResponse(parts=[TextPart(json.dumps(GOOD))])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, GOOD)])

    return FunctionModel(respond)


def test_the_first_working_model_answers_and_is_journalled(engine: Engine) -> None:
    router = _router(engine, _ok_model)
    result = router.call("r1", "trader", "decide", Verdict)

    assert result.output.side == "long"
    assert result.attempts == 1
    rows = _rows(engine)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["agent"] == "trader" and rows[0]["prompt"] == "decide"
    assert rows[0]["latency_ms"] is not None


def test_a_failing_model_is_recorded_and_the_next_one_is_tried(engine: Engine) -> None:
    calls: list[str] = []

    def factory(ref: ModelRef) -> Any:
        calls.append(ref.label)
        if len(calls) == 1:
            raise RuntimeError("provider exploded")
        return _ok_model(ref)

    router = _router(engine, factory)
    result = router.call("r1", "trader", "decide", Verdict)

    assert result.attempts == 2
    rows = _rows(engine)
    assert [r["status"] for r in rows] == ["error", "ok"]
    assert "provider exploded" in rows[0]["error"]


def test_a_rate_limited_provider_does_not_trip_its_circuit_breaker(engine: Engine) -> None:
    """429 means "not now", not "broken" — opening the breaker would punish a healthy provider."""
    seen: list[str] = []

    def factory(ref: ModelRef) -> Any:
        seen.append(ref.provider)
        if len(seen) == 1:
            raise ModelHTTPError(429, ref.model)
        return _ok_model(ref)

    router = _router(engine, factory)
    router.call("r1", "trader", "decide", Verdict)

    assert _rows(engine)[0]["status"] == "rate_limited"
    assert router.breakers.get(f"llm:{seen[0]}").state == "closed"


def test_a_broken_provider_does_trip_its_breaker(engine: Engine) -> None:
    seen: list[str] = []

    def factory(ref: ModelRef) -> Any:
        seen.append(ref.provider)
        if len(seen) == 1:
            raise RuntimeError("500 from upstream")
        return _ok_model(ref)

    router = _router(engine, factory)
    router.call("r1", "trader", "decide", Verdict)
    # A genuine fault counts against the provider; enough of them and it stops being offered.
    assert router.breakers.get(f"llm:{seen[0]}").attempts >= 1


def test_when_every_model_fails_the_caller_is_told_loudly(engine: Engine) -> None:
    def factory(ref: ModelRef) -> Any:
        raise RuntimeError("everything is down")

    router = _router(engine, factory)
    with pytest.raises(AllModelsFailedError) as exc:
        router.call("r1", "trader", "decide", Verdict)

    assert "trader" in str(exc.value)
    rows = _rows(engine)
    assert rows and all(r["status"] == "error" for r in rows)


def test_the_attempt_is_counted_before_the_call_returns(engine: Engine) -> None:
    """The per-minute window must include in-flight calls, or a burst slips through while every
    request is still pending."""
    counted: list[int] = []

    def factory(ref: ModelRef) -> Any:
        counted.append(router.calls_last_minute(ref))
        return _ok_model(ref)

    router = _router(engine, factory)
    router.call("r1", "trader", "decide", Verdict)
    assert counted == [1], "the attempt must already be on the window when the call is made"


def test_a_second_opinion_avoids_the_first_model_s_family(engine: Engine) -> None:
    router = _router(engine, _ok_model)
    first = router.call("r1", "trader", "decide", Verdict)
    second = router.call(
        "r1", "consensus", "decide", Verdict, used_families={"trader": first.ref.family}
    )
    assert second.ref.family != first.ref.family


def test_a_call_gives_up_before_it_eats_the_run_budget(engine: Engine) -> None:
    """Measured live 2026-09-20: 15 assets x 8 candidates x a 45s timeout took 7m29s against an
    8-minute budget. One stubborn asset must not be able to spend the whole run."""
    tried: list[str] = []

    def factory(ref: ModelRef) -> Any:
        tried.append(ref.label)
        raise RuntimeError("down")

    router = _router(engine, factory)
    with pytest.raises(AllModelsFailedError):
        router.call("r1", "trader", "decide", Verdict)
    assert len(tried) <= 4, f"tried {len(tried)} models before giving up"


def test_an_output_token_limit_is_always_set(engine: Engine) -> None:
    """Cloudflare's gpt-oss-120b answered "token limit (provider default) exceeded before any
    response was generated" — a reasoning model spends an unset budget on thinking."""
    seen: list[dict[str, Any]] = []

    class Recorder(TestModel):
        def __init__(self) -> None:
            super().__init__(custom_output_args={"side": "long", "score": 4})

    def factory(ref: ModelRef) -> Any:
        return Recorder()

    router = _router(engine, factory)
    settings = router.model_settings("trader")
    seen.append(settings)
    assert settings["max_tokens"] >= 512


def test_a_call_stops_when_its_wall_clock_deadline_passes(engine: Engine) -> None:
    """owner's rule: no role waits more than ten minutes. Attempt count alone does not bound time —
    four models x a 45s timeout, each with a repair, is minutes of wall clock per asset."""
    import time as clock

    tried: list[str] = []

    def slow(ref: ModelRef) -> Any:
        tried.append(ref.label)
        clock.sleep(0.05)
        raise RuntimeError("down")

    router = _router(engine, slow)
    router.deadline_s = 0.06  # one attempt's worth
    with pytest.raises(AllModelsFailedError) as exc:
        router.call("r1", "trader", "decide", Verdict)

    assert len(tried) < 4, "the deadline must cut the chain short"
    assert "deadline" in str(exc.value).lower()


def test_a_rate_limited_provider_is_skipped_for_the_rest_of_the_run(engine: Engine) -> None:
    """Measured 2026-09-20: a consensus run failed on every asset because all four attempts
    landed on providers already returning 429, while healthy ones sat further down the list.

    Our quota counters only know about calls WE made; a provider exhausted by yesterday's
    testing, or shared with another project, looks fresh to them. A 429 is the provider telling
    us the truth, and it should not have to tell us fifteen times.
    """
    tried: list[str] = []

    def factory(ref: ModelRef) -> Any:
        tried.append(ref.provider)
        if ref.provider == "gemini":
            raise ModelHTTPError(429, ref.model)
        return _ok_model(ref)

    router = _router(engine, factory)
    router.call("r1", "analyst", "decide", Verdict)  # analyst leads with gemini
    first_round = list(tried)
    tried.clear()

    router.call("r1", "analyst", "decide", Verdict)
    assert "gemini" in first_round
    assert "gemini" not in tried, "a provider that just said 429 should not be asked again"


def test_the_cooldown_expires_rather_than_banning_a_provider(engine: Engine) -> None:
    """A per-minute limit clears in a minute. Treating it as permanent would throw away the
    provider for the whole run over one busy moment."""
    router = _router(engine, _ok_model)
    ref = router.plan("analyst")[0]
    tier = "strong_json"
    router.record_rate_limit(ref, seconds=0.01)
    assert ref.provider not in {c.provider for c in router.candidates(tier)}

    import time as clock

    clock.sleep(0.05)
    assert ref.provider in {c.provider for c in router.candidates(tier)}


def test_a_daily_allocation_message_cools_down_for_longer(engine: Engine) -> None:
    """ "You have used up your daily free allocation" is not a busy minute, and retrying in
    sixty seconds wastes an attempt every single time."""
    from argus.llm.router import cooldown_seconds

    assert cooldown_seconds("Rate limit exceeded") <= 120
    assert cooldown_seconds("you have used up your daily free allocation of 10,000 neurons") >= 3600


# --- L-6: every HTTP request is counted ------------------------------------------------------
def _scripted(replies: list[dict[str, Any]], hits: list[str]) -> Any:
    """A model that answers with `replies` in order (the last one repeats), counting requests.

    A FunctionModel rather than a TestModel because the point is to count what reaches the
    "provider": Pydantic AI's own output retries happen inside one `run_sync`, invisibly.
    """
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        hits.append("request")
        args = replies[min(len(hits), len(replies)) - 1]
        if not info.output_tools:  # Groq is asked for prompted JSON, not a tool call
            return ModelResponse(parts=[TextPart(json.dumps(args))])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond)


def _attempts(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM llm_attempts")).scalar() or 0)


BAD = {"side": "long", "score": "not a number"}
GOOD = {"side": "long", "score": 4}


def test_every_http_request_is_counted_against_the_quota(engine: Engine) -> None:
    """L-6: Pydantic AI retries invalid output once by default, and the router repairs once on
    top — up to four requests per journaled attempt, while headroom counted one. A provider
    metered per request (or Groq, per token) then refuses us earlier than the router expects."""
    hits: list[str] = []
    router = _router(engine, lambda ref: _scripted([BAD], hits))
    with pytest.raises(AllModelsFailedError):
        router.call("r1", "trader", "decide", Verdict)

    candidates = len(_rows(engine))
    assert len(hits) == _attempts(engine), "every request must be on the per-minute window"
    assert len(hits) == 2 * candidates, "one first try and one repair per model, no hidden retries"


def test_a_repair_that_succeeds_costs_exactly_two_requests(engine: Engine) -> None:
    hits: list[str] = []
    router = _router(engine, lambda ref: _scripted([BAD, GOOD], hits))
    result = router.call("r1", "trader", "decide", Verdict)

    assert result.repaired and result.output.score == 4
    assert len(hits) == 2 == _attempts(engine)


@pytest.fixture
def refusing_server() -> Any:
    """A localhost "provider" that refuses every request with a set status, counting each one.

    Offline: it binds 127.0.0.1 only. It exists because a FunctionModel cannot see the HTTP
    client's own retries — they happen below Pydantic AI, inside the OpenAI SDK.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[str] = []
    state = {"status": 503}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = b'{"error": {"message": "refused by the test server"}}'
            self.send_response(state["status"])
            self.send_header("content-type", "application/json")
            # Lets an SDK retry fire at once, so the bug costs milliseconds to observe.
            self.send_header("retry-after-ms", "1")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", hits, state
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status", [503, 429])
def test_the_http_client_adds_no_requests_the_quota_cannot_see(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, refusing_server: Any, status: int
) -> None:
    """L-6: the OpenAI SDK retries 408/409/429/5xx and timeouts twice by default, inside one
    run_sync. Against a 503 the router journaled four attempts while twelve requests went out;
    a 429 was retried behind the router's back instead of starting its cooldown, and a paid
    timeout could be billed three times for one charged ceiling (L-7)."""
    import copy

    import argus.llm.router as router_module
    from argus.config import models_config
    from argus.llm.router import build_model

    url, hits, state = refusing_server
    state["status"] = status
    config = copy.deepcopy(models_config())
    # Real OpenAI-compatible models, every one pointed at the local server. No native provider
    # is in the tier, so nothing here can leave the machine.
    local = ["groq", "openrouter", "cloudflare", "zhipu"]
    for name in local:
        config["providers"][name]["base_url"] = url
    # Free IDs, so the paid cap does not withhold the OpenRouter and Zhipu entries.
    config["tiers"]["strong_json"] = [
        "groq/test-model",
        "openrouter/test-model:free",
        "cloudflare/test-model",
        "zhipu/glm-4.7-flash",
    ]
    monkeypatch.setattr(router_module, "models_config", lambda: config)

    def factory(ref: ModelRef) -> Any:
        assert ref.provider in local, f"{ref.label} would leave the machine"
        return build_model(ref, router.settings)

    router = _router(engine, factory)
    with pytest.raises(AllModelsFailedError):
        router.call("r1", "trader", "decide", Verdict)

    assert len(_rows(engine)) == len(local)
    assert len(hits) == _attempts(engine) == len(local), "one HTTP request per counted attempt"


# --- L-11: config that nothing reads ---------------------------------------------------------
def test_every_router_policy_key_has_a_reader() -> None:
    """L-11: `skip_provider_at_rpd_fraction` and `repair_attempts` sat in models.yaml while the
    router hard-coded both. A knob that does nothing is worse than no knob: someone turns it,
    sees no change in tests, and believes the system is configured as the file says."""
    import argus.llm.golden as golden
    import argus.llm.router as router_module
    from argus.config import models_config

    readers = Path(router_module.__file__).read_text() + Path(golden.__file__).read_text()
    unread = [key for key in models_config()["policy"] if f'"{key}"' not in readers]
    assert not unread, f"policy keys nobody reads: {unread}"


def test_repair_attempts_comes_from_config(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import copy

    import argus.llm.router as router_module
    from argus.config import models_config

    config = copy.deepcopy(models_config())
    config["policy"]["repair_attempts"] = 0
    monkeypatch.setattr(router_module, "models_config", lambda: config)

    hits: list[str] = []
    router = _router(engine, lambda ref: _scripted([BAD], hits))
    with pytest.raises(AllModelsFailedError):
        router.call("r1", "trader", "decide", Verdict)
    assert len(hits) == len(_rows(engine)), "no repair when the config allows none"


def test_a_prompt_larger_than_a_provider_s_token_budget_skips_it_without_an_attempt(
    engine: Engine,
) -> None:
    """2026-10-08 00:32: the batched Trader prompt (~17-20k tokens) went to Groq after Gemini
    failed. Groq's free tier allows 8000 tokens a minute, so both Groq models answered 413
    'Request too large' — certain failures that used two of the four attempts, and Mistral, the
    family that could have answered, was never tried. A request that cannot fit a provider's
    per-minute budget is skipped before it is sent and costs no attempt."""
    from argus.llm.router import agent_policy, approx_tokens

    big = "x" * (4 * 20_000)  # ~20k tokens, as the batched Trader prompt
    assert approx_tokens(big) > 8_000
    tried: list[str] = []

    def factory(ref: ModelRef) -> Any:
        tried.append(ref.label)
        if ref.provider == "gemini":
            raise RuntimeError("schema failure")
        return _ok_model(ref)

    router = _router(engine, factory)
    result = router.call("r1", "trader", big, Verdict)

    assert not any(label.startswith("groq/") for label in tried), tried
    assert result.ref.family in agent_policy("trader").families
    assert result.ref.provider != "groq"
    assert all(r["provider"] != "groq" for r in _rows(engine))


def test_a_small_prompt_still_reaches_groq(engine: Engine) -> None:
    """The counterpart: the guard must not quietly drop Groq for the roles it serves well."""
    tried: list[str] = []

    def factory(ref: ModelRef) -> Any:
        tried.append(ref.label)
        if ref.provider == "gemini":
            raise RuntimeError("schema failure")
        return _ok_model(ref)

    _router(engine, factory).call("r1", "trader", "decide", Verdict)
    assert any(label.startswith("groq/") for label in tried), tried


def test_a_call_refused_by_an_open_breaker_does_not_burn_quota(engine: Engine) -> None:
    """A refused call sends no request, so it must not count against the provider's window."""
    router = _router(engine, _ok_model)
    first_key: dict[str, str] = {}
    real_get = router.breakers.get

    class _Open:
        def would_allow(self) -> bool:
            return True

        def allow(self, force: bool = False) -> bool:
            return False

    def get(key: str) -> Any:
        first_key.setdefault("key", key)
        return _Open() if key == first_key["key"] else real_get(key)

    router.breakers.get = get  # type: ignore[method-assign]
    attempts: list[str] = []
    real_record = router.record_attempt

    def record(ref: ModelRef, at_offset_s: float = 0.0) -> None:
        attempts.append(ref.breaker_key)
        real_record(ref, at_offset_s)

    router.record_attempt = record  # type: ignore[method-assign]
    router.call("r1", "trader", "decide", Verdict)
    assert first_key["key"] not in attempts
