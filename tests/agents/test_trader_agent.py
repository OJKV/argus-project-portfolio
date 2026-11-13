"""The Trader agent: dossier in, a validated plan or an explicit NO_TRADE out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import Engine, text

from argus.config import Settings
from argus.exchange.breakers import BreakerRegistry
from argus.features.universe import Asset
from argus.journal.db import open_journal
from argus.llm.agents.trader import propose, to_plan_input
from argus.llm.router import Router
from argus.llm.schemas import FactorScores, Target, TradePlan
from tests.runs.test_manager_shadow import _dossier, _market, _regime

PLAN_ARGS = {
    "symbol": "BTC",
    "side": "long",
    "entry_kind": "limit",
    "entry_low": 99.0,
    "entry_high": 101.0,
    "stop": 94.0,
    "targets": [{"price": 110.0, "close_pct": 50}, {"price": 120.0, "close_pct": 50}],
    "time_stop_hours": 120,
    "scores": {"fundamental": 0, "onchain": 0, "macro": 0, "catalyst": 1, "technical": 2},
    "evidence": {"catalyst": ["catalysts.next"], "technical": ["technical.price"]},
    "thesis": "trend continuation",
}


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_journal(tmp_path / "journal.db")
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES ('r1','manager','demo','2026-09-20T00:00:00+00:00','RUNNING',1,'-')"
            )
        )
    return eng


def _model(output: Any) -> FunctionModel:
    """A model that answers with the single `final_result` output tool."""

    def respond(messages: Any, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=output)])

    return FunctionModel(respond)


def _router(engine: Engine, output: Any) -> Router:
    settings = Settings(
        _env_file=None,
        gemini_api_key="g",
        groq_api_key="g",
        mistral_api_key="g",
        cloudflare_api_token="g",
        cloudflare_account_id="a",
        openrouter_api_key="g",
        zhipu_api_key="g",
    )
    return Router(
        engine=engine,
        settings=settings,
        breakers=BreakerRegistry(),
        model_factory=lambda ref: _model(output),
    )


def _inputs() -> tuple[Any, Any]:
    regime = _regime()
    asset = Asset("BTC", "BTC-USDT-SWAP", "bitcoin", "core")
    return _dossier(asset, regime), _market("BTC", regime)


def test_a_plan_comes_back_validated_and_journalled(engine: Engine) -> None:
    dossier, market = _inputs()
    result = propose(
        _router(engine, {"decision": "trade", "plan": PLAN_ARGS}), "r1", dossier, market
    )

    assert result.output.plan is not None
    assert result.output.plan.symbol == "BTC"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM llm_calls")).scalar() == 1


def test_the_prompt_actually_carries_the_dossier(engine: Engine) -> None:
    dossier, market = _inputs()
    propose(_router(engine, {"decision": "trade", "plan": PLAN_ARGS}), "r1", dossier, market)  # noqa: E501
    with engine.connect() as conn:
        prompt = conn.execute(text("SELECT prompt FROM llm_calls")).scalar()
    assert "BTC" in str(prompt)
    assert "atr_4h_pct" in str(prompt) or "atr" in str(prompt).lower()


def test_no_trade_is_returned_not_treated_as_a_failure(engine: Engine) -> None:
    dossier, market = _inputs()
    router = _router(engine, {"decision": "no_trade", "reason": "no edge"})
    result = propose(router, "r1", dossier, market)
    assert result.output.plan is None and result.output.reason == "no edge"


def test_the_plan_converts_into_what_the_gate_expects() -> None:
    plan = TradePlan(
        **{
            **PLAN_ARGS,
            "targets": [Target(price=110.0, close_pct=50), Target(price=120.0, close_pct=50)],
            "scores": FactorScores(fundamental=0, onchain=0, macro=0, catalyst=1, technical=2),
        }
    )
    gate_input = to_plan_input(plan)
    assert gate_input.symbol == "BTC"
    assert gate_input.stop == 94.0
    assert [t.close_pct for t in gate_input.targets] == [50, 50]
    assert gate_input.scores.total == 3
