"""Router reliability fixes from the 2026-10-26 Logfire read.

What the traces showed: thinking models stopped at the 2000-token output cap before answering;
Groq answered 400 ("Failed to call a function", "Tool choice is required") to every structured
call because its models cannot be forced into a tool call, and each fell through to Cloudflare
after a 45 s timeout; and every agent span was named "invoke_agent agent".

Output retries are deliberately NOT here: the router's own repair (`policy.repair_attempts`,
default 1) is the output retry, counted against the quota (L-6). `test_router_call.py` pins that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pydantic_ai
import pytest
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import Engine, text

from argus.config import Settings, models_config
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


def _router(engine: Engine, factory: Any = None) -> Router:
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
    kwargs: dict[str, Any] = {"model_factory": factory} if factory else {}
    return Router(engine=engine, settings=settings, breakers=BreakerRegistry(), **kwargs)


# --- 2. per-agent output cap ------------------------------------------------------------------
def test_the_trader_gets_a_4000_token_output_cap_and_others_keep_the_default(
    engine: Engine,
) -> None:
    router = _router(engine)
    assert router.model_settings("trader")["max_tokens"] == 4000
    default = int(models_config()["policy"]["max_tokens"])
    for agent in ("regime", "consensus", "reviewer", "redteam", "riskdebate", "analyst"):
        assert router.model_settings(agent)["max_tokens"] == default, agent


def test_the_provider_ceiling_still_lowers_the_traders_cap(engine: Engine) -> None:
    """Groq refuses a request whose max_tokens alone exceeds its per-minute output limit."""
    assert _router(engine).model_settings("trader", "groq")["max_tokens"] == 800


def test_openai_reasoning_models_get_an_output_floor(engine: Engine) -> None:
    """2026-10-27: gpt-5 spent the whole 2000-token budget reasoning and returned nothing on the
    consensus fallback. A provider floor raises it; it never lowers anyone's cap."""
    router = _router(engine)
    for agent in ("consensus", "redteam", "trader"):
        assert router.model_settings(agent, "openai")["max_tokens"] >= 8000, agent
    assert router.model_settings("consensus")["max_tokens"] == int(
        models_config()["policy"]["max_tokens"]
    )


def test_deepseek_answers_in_prompted_json() -> None:
    """2026-10-27: DeepSeek 400 "Thinking mode does not support" a forced tool call."""
    assert models_config()["providers"]["deepseek"].get("output_mode") == "prompted"


# --- 3. Groq: prompted JSON, not a forced tool call -------------------------------------------
def _script(seen: list[tuple[str, int, str]]) -> Any:
    """A tool call when the request offers an output tool, else JSON text."""

    def factory(ref: ModelRef) -> Any:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append((ref.provider, len(info.output_tools), str(info.instructions or "")))
            if info.output_tools:
                return ModelResponse(
                    parts=[ToolCallPart(info.output_tools[0].name, {"side": "long", "score": 4})]
                )
            return ModelResponse(parts=[TextPart('{"side": "long", "score": 4}')])

        return FunctionModel(respond)

    return factory


def test_groq_is_asked_for_prompted_json_with_no_output_tool(engine: Engine) -> None:
    seen: list[tuple[str, int, str]] = []
    router = _router(engine, _script(seen))
    result = router.call("r1", "reviewer", "decide", Verdict)  # groq leads the reasoning tier

    assert result.ref.provider == "groq"
    assert result.output == Verdict(side="long", score=4), "the text answer is still validated"
    provider, output_tools, instructions = seen[0]
    assert provider == "groq" and output_tools == 0, "no tool call for Groq to refuse"
    assert "score" in instructions, "the schema travels in the prompt instead"


def test_other_providers_still_get_a_forced_output_tool(engine: Engine) -> None:
    seen: list[tuple[str, int, str]] = []
    router = _router(engine, _script(seen))
    router.call("r1", "regime", "decide", Verdict)  # google leads the fast tier
    assert seen[0][0] == "gemini" and seen[0][1] == 1


def test_a_groq_answer_that_breaks_the_schema_is_rejected_not_trusted(engine: Engine) -> None:
    """Prompted output must keep the validation the tool path gave us (rule 6)."""
    seen: list[str] = []

    def factory(ref: ModelRef) -> Any:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen.append(ref.provider)
            return ModelResponse(parts=[TextPart('{"side": "long", "score": "lots"}')])

        return FunctionModel(respond)

    router = _router(engine, factory)
    with pytest.raises(AllModelsFailedError):
        router.call("r1", "reviewer", "decide", Verdict)
    assert "groq" in seen
    with engine.connect() as conn:
        statuses = {
            row.status
            for row in conn.execute(text("SELECT status FROM llm_calls WHERE provider = 'groq'"))
        }
    assert statuses == {"schema_fail"}


# --- 4. span names ---------------------------------------------------------------------------
def test_each_agent_span_carries_the_agents_role_name(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str | None] = []
    real = pydantic_ai.Agent

    class Spy(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            names.append(kwargs.get("name"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pydantic_ai, "Agent", Spy)
    router = _router(engine, _script([]))
    router.call("r1", "trader", "decide", Verdict)
    router.call("r1", "consensus", "decide", Verdict)
    assert names == ["trader", "consensus"]
