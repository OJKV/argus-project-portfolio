"""The paid cap is code, not configuration (L-7).

`paid_llm_monthly_cap_usd` existed with no reader, `ModelRef.paid` was never consulted and
`llm_calls.tokens_in/out` were always NULL — so one edit to `tiers:` in models.yaml would have
spent with no ceiling. These tests put a paid model in a tier on purpose and check the router
refuses it once the month's spend reaches the cap, and refuses it outright when it cannot price
a call at all.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import Engine, text

import argus.llm.router as router_module
from argus.config import Settings, models_config
from argus.exchange.breakers import BreakerRegistry
from argus.journal.db import open_journal, utc_now
from argus.llm.router import ModelRef, Router

PAID = "deepseek/deepseek-flash"
# Test prices only — round numbers so the arithmetic in each case is obvious.
PRICE = {"in_per_mtok": 1.0, "out_per_mtok": 2.0}


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


@pytest.fixture
def paid_first(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The edit L-7 is about: a paid model put at the head of a live tier."""
    config = copy.deepcopy(models_config())
    config["tiers"]["strong_json"] = [PAID, *config["tiers"]["strong_json"]]
    config["prices"] = {PAID: dict(PRICE)}
    monkeypatch.setattr(router_module, "models_config", lambda: config)
    return config


def _router(engine: Engine, cap: float = 2.0, factory: Any = None) -> Router:
    settings = Settings(
        _env_file=None,
        gemini_api_key="g",
        groq_api_key="g",
        mistral_api_key="g",
        deepseek_api_key="d",
        paid_llm_monthly_cap_usd=cap,
    )
    kwargs: dict[str, Any] = {"model_factory": factory} if factory else {}
    return Router(engine=engine, settings=settings, breakers=BreakerRegistry(), **kwargs)


def _spend(
    engine: Engine, tokens_in: int | None, tokens_out: int | None, prompt: str = "p"
) -> None:
    provider, model = PAID.split("/", 1)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO llm_calls (call_id, run_id, created_at, agent, tier, provider, model,"
                " family, attempt, status, tokens_in, tokens_out, prompt)"
                " VALUES (:c, 'r1', :t, 'trader', 'strong_json', :p, :m, 'deepseek', 1, 'ok',"
                " :i, :o, :pr)"
            ),
            {
                "c": f"c{tokens_in}{tokens_out}{len(prompt)}",
                "t": utc_now(),
                "p": provider,
                "m": model,
                "i": tokens_in,
                "o": tokens_out,
                "pr": prompt,
            },
        )


def _offered(router: Router) -> set[str]:
    return {ref.label for ref in router.candidates("strong_json")}


def test_a_paid_model_is_offered_while_the_month_is_under_its_cap(
    engine: Engine, paid_first: dict[str, Any]
) -> None:
    _spend(engine, tokens_in=100_000, tokens_out=100_000)  # $0.10 + $0.20
    router = _router(engine, cap=2.0)
    assert router.month_to_date_spend_usd() == pytest.approx(0.30)
    assert PAID in _offered(router)


def test_a_paid_model_is_withheld_once_the_month_s_spend_reaches_the_cap(
    engine: Engine, paid_first: dict[str, Any]
) -> None:
    _spend(engine, tokens_in=1_000_000, tokens_out=500_000)  # $1.00 + $1.00
    router = _router(engine, cap=2.0)
    assert PAID not in _offered(router)
    # The free models in the same tier are untouched: the cap is about money, not the tier.
    assert _offered(router), "the free models must still be offered"


def test_a_paid_model_with_no_price_is_never_offered(
    engine: Engine, paid_first: dict[str, Any]
) -> None:
    """A cap that cannot price a call is not a cap."""
    paid_first["prices"] = {}
    assert PAID not in _offered(_router(engine, cap=1000.0))


def test_an_attempt_with_unknown_usage_is_charged_at_its_ceiling(
    engine: Engine, paid_first: dict[str, Any]
) -> None:
    """A failed or timed-out paid call may still have been billed. With no usage recorded, it is
    charged as if the prompt were one token per character and the answer hit max_tokens —
    over-counting reaches the cap early, which is the safe direction."""
    _spend(engine, tokens_in=None, tokens_out=None, prompt="x" * 1000)
    ceiling = int(paid_first["policy"]["max_tokens_by_agent"]["trader"])  # the row is a trader call
    expected = (1000 * PRICE["in_per_mtok"] + ceiling * PRICE["out_per_mtok"]) / 1e6
    assert _router(engine).month_to_date_spend_usd() == pytest.approx(expected)


def test_token_usage_is_journaled_for_every_call(engine: Engine) -> None:
    """Without tokens there is no spend to count; the columns were always NULL."""

    def factory(ref: ModelRef) -> Any:
        return TestModel(custom_output_args={"side": "long", "score": 4})

    router = _router(engine, factory=factory)
    router.call("r1", "trader", "decide", Verdict)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT tokens_in, tokens_out FROM llm_calls")).one()
    assert row.tokens_in and row.tokens_in > 0
    assert row.tokens_out and row.tokens_out > 0


# --- paid is a property of the model, not only of the provider --------------------------------
# OpenRouter and Zhipu sell free and paid models under one key. models.yaml records both cases:
# glm-5.3-flash is Zhipu's cheapest *paid* model, and nex-n2.5-mini is now paid-only on
# OpenRouter. Neither provider is `paid: true`, so a provider-level flag let either model into a
# tier uncapped and uncounted.
MIXED_PAID = ["openrouter/nex-agi/nex-n2.5-mini", "zhipu/glm-5.3-flash"]


@pytest.fixture
def mixed_paid_first(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = copy.deepcopy(models_config())
    config["tiers"]["strong_json"] = [*MIXED_PAID, *config["tiers"]["strong_json"]]
    config["prices"] = {label: dict(PRICE) for label in MIXED_PAID}
    monkeypatch.setattr(router_module, "models_config", lambda: config)
    return config


def _mixed_router(engine: Engine, cap: float) -> Router:
    settings = Settings(
        _env_file=None,
        gemini_api_key="g",
        groq_api_key="g",
        openrouter_api_key="o",
        zhipu_api_key="z",
        # A provider with no entry has a cap of zero, so these two must be named to be offered.
        paid_monthly_cap_usd_by_provider={"openrouter": cap, "zhipu": cap},
        paid_llm_monthly_cap_usd=cap,
    )
    return Router(engine=engine, settings=settings, breakers=BreakerRegistry())


def _spend_on(engine: Engine, label: str, tokens_in: int, tokens_out: int) -> None:
    provider, model = label.split("/", 1)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO llm_calls (call_id, run_id, created_at, agent, tier, provider, model,"
                " family, attempt, status, tokens_in, tokens_out, prompt)"
                " VALUES (:c, 'r1', :t, 'trader', 'strong_json', :p, :m, :p, 1, 'ok', :i, :o, 'p')"
            ),
            {"c": label, "t": utc_now(), "p": provider, "m": model}
            | {"i": tokens_in, "o": tokens_out},
        )


def test_spend_on_a_paid_model_of_a_mixed_provider_is_counted(
    engine: Engine, mixed_paid_first: dict[str, Any]
) -> None:
    for label in MIXED_PAID:
        _spend_on(engine, label, tokens_in=100_000, tokens_out=100_000)  # $0.30 each
    assert _mixed_router(engine, cap=2.0).month_to_date_spend_usd() == pytest.approx(0.60)


def test_a_paid_model_of_a_mixed_provider_is_withheld_at_the_cap(
    engine: Engine, mixed_paid_first: dict[str, Any]
) -> None:
    assert set(MIXED_PAID) <= _offered(_mixed_router(engine, cap=2.0)), "offered under the cap"

    _spend_on(engine, MIXED_PAID[0], tokens_in=1_000_000, tokens_out=500_000)  # $2.00
    offered = _offered(_mixed_router(engine, cap=2.0))
    assert not set(MIXED_PAID) & offered
    assert offered, "the free models must still be offered"


def test_an_unpriced_paid_model_of_a_mixed_provider_is_never_offered(
    engine: Engine, mixed_paid_first: dict[str, Any]
) -> None:
    mixed_paid_first["prices"] = {}
    assert not set(MIXED_PAID) & _offered(_mixed_router(engine, cap=1000.0))


def test_the_free_models_of_a_mixed_provider_stay_free(engine: Engine) -> None:
    """They have no price, so a rule that called them paid would silently withhold them."""
    mixed = [
        ref
        for ref in router_module.tier_refs("reasoning")
        if ref.provider in {"openrouter", "zhipu"}
    ]
    assert mixed, "the reasoning tier routes to free OpenRouter and Zhipu models"
    assert not [ref.label for ref in mixed if ref.paid]
    offered = {ref.label for ref in _mixed_router(engine, cap=0.0).candidates("reasoning")}
    assert {ref.label for ref in mixed} <= offered
