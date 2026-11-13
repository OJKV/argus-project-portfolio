"""The paid tier is the last link of every agent's chain, never an early one.

DeepSeek and OpenAI cost money per call. They exist for the day every free provider is down at
once, not for a busy Tuesday. owner's rule (2026-10-26): when an agent's free models all fail,
it may
fall back to the PRICED DeepSeek models and then the PRICED OpenAI models, for EVERY agent, with
each provider capped at $5/month and an overall ceiling of $10. These tests assert the structural
guarantees; the sustained load test that proves it against live quotas lives in `scripts` and
runs before any 24/7 demo.
"""

from __future__ import annotations

import copy
import math
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
from argus.llm.router import (
    AllModelsFailedError,
    ModelRef,
    Router,
    agent_policy,
    limits_for,
    tier_refs,
)

AGENTS = (
    "regime",
    "catalyst",
    "analyst",
    "redteam",
    "riskdebate",
    "trader",
    "consensus",
    "reviewer",
    "postmortem",
    "reflector",
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _router(
    engine: Engine,
    cap: float = 10.0,
    factory: Any = None,
    by_provider: dict[str, float] | None = None,
) -> Router:
    kwargs: dict[str, Any] = {"model_factory": factory} if factory else {}
    caps: dict[str, Any] = (
        {"paid_monthly_cap_usd_by_provider": by_provider} if by_provider is not None else {}
    )
    return Router(
        engine=engine,
        **kwargs,
        settings=Settings(
            _env_file=None,
            gemini_api_key="g",
            groq_api_key="g",
            openrouter_api_key="g",
            mistral_api_key="g",
            cloudflare_api_token="g",
            cloudflare_account_id="a",
            zhipu_api_key="g",
            deepseek_api_key="g",
            openai_api_key="g",
            paid_llm_monthly_cap_usd=cap,
            **caps,
        ),
        breakers=BreakerRegistry(),
    )


# owner 2026-10-27: the cheapest model of each provider only.
PAID_TAIL = [
    "deepseek/deepseek-flash",
    "openai/gpt-5-nano",
]
HUGE = {"deepseek": 1_000_000.0, "openai": 1_000_000.0}


def _paid(router: Router, agent: str = "trader", **kwargs: Any) -> list[str]:
    return [r.label for r in router.plan(agent, **kwargs) if r.paid]


def _seed_run(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO runs (run_id, kind, mode, started_at, status, shadow,"
                " config_hash) VALUES ('r1', 'manager', 'demo', '2026-09-20T00:00:00+00:00',"
                " 'RUNNING', 1, '-')"
            )
        )


def _spend(
    engine: Engine,
    call_id: str,
    provider: str,
    model: str,
    tokens_in: int | None,
    tokens_out: int | None,
    prompt: str = "p",
) -> None:
    _seed_run(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO llm_calls (call_id, run_id, created_at, agent, tier, provider, model,"
                " family, attempt, status, tokens_in, tokens_out, prompt)"
                " VALUES (:id, 'r1', :t, 'trader', 'strong_json', :p, :m, :p, 1, 'ok', :i, :o,"
                " :prompt)"
            ),
            {
                "id": call_id,
                "t": utc_now(),
                "p": provider,
                "m": model,
                "i": tokens_in,
                "o": tokens_out,
                "prompt": prompt,
            },
        )


def test_the_caps_default_to_five_dollars_a_provider_and_ten_overall() -> None:
    settings = Settings(_env_file=None)
    assert settings.paid_monthly_cap_usd_by_provider == {"deepseek": 5.0, "openai": 5.0}
    assert settings.paid_llm_monthly_cap_usd == 10.0


def test_every_agent_opts_in_to_the_paid_last_resort() -> None:
    assert {a for a in AGENTS if agent_policy(a).paid_last_resort} == set(AGENTS)


@pytest.mark.parametrize("agent", AGENTS)
def test_every_chain_is_free_first_and_paid_last(engine: Engine, agent: str) -> None:
    plan = _router(engine).plan(agent)
    flags = [ref.paid for ref in plan]
    assert any(flags), f"{agent} must reach the paid last resort"
    first_paid = flags.index(True)
    assert first_paid >= 5, f"{agent}: the free chain is not cut short to make room"
    assert not any(flags[:first_paid]) and all(flags[first_paid:]), "free models come first"
    assert [r.label for r in plan[first_paid:]] == PAID_TAIL, "DeepSeek first, then OpenAI"


@pytest.mark.parametrize("agent", AGENTS)
def test_nothing_paid_is_offered_when_both_caps_are_reached(engine: Engine, agent: str) -> None:
    router = _router(engine)
    _spend(engine, "c1", "deepseek", "deepseek-flash", 10_000_000, 2_000_000)  # $5.40
    _spend(engine, "c2", "openai", "gpt-5-mini", 20_000_000, 0)  # $5.00
    assert _paid(router, agent) == []
    assert router.plan(agent), "the free models are untouched by the caps"


@pytest.mark.parametrize("agent", AGENTS)
def test_nothing_paid_is_offered_without_prices(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    config = copy.deepcopy(models_config())
    config["prices"] = {}
    monkeypatch.setattr(router_module, "models_config", lambda: config)
    assert _paid(_router(engine, cap=1e9, by_provider=HUGE), agent) == []


@pytest.mark.parametrize("agent", AGENTS)
def test_an_unpriced_paid_model_is_never_offered(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    config = copy.deepcopy(models_config())
    del config["prices"]["openai/gpt-5-nano"]
    monkeypatch.setattr(router_module, "models_config", lambda: config)
    assert _paid(_router(engine), agent) == ["deepseek/deepseek-flash"]


def test_a_provider_without_a_cap_is_never_offered(engine: Engine) -> None:
    router = _router(engine, by_provider={"deepseek": 5.0})
    assert _paid(router) == PAID_TAIL[:1], "no cap entry for openai means a cap of zero"


def test_openai_backs_up_deepseek_when_deepseek_has_no_key(engine: Engine) -> None:
    router = _router(engine)
    router.settings = router.settings.model_copy(update={"deepseek_api_key": None})
    assert _paid(router) == PAID_TAIL[1:]


@pytest.mark.parametrize("agent", ["redteam", "consensus", "reviewer"])
def test_a_judge_never_shares_the_traders_paid_family(engine: Engine, agent: str) -> None:
    """Adversarial pairing holds in the paid tail: the family that made the trade is excluded."""
    router = _router(engine)
    assert _paid(router, agent, used_families={"trader": "deepseek"}) == PAID_TAIL[1:]
    assert _paid(router, agent, used_families={"trader": "openai"}) == PAID_TAIL[:1]
    # An unrelated trader family removes nothing.
    assert _paid(router, agent, used_families={"trader": "google"}) == PAID_TAIL


def test_consensus_differs_from_the_trader_when_both_fall_back_to_paid(engine: Engine) -> None:
    def factory(ref: ModelRef) -> Any:
        if not ref.paid:
            raise RuntimeError("free model down")
        if ref.provider == "deepseek":  # prompted JSON, not a tool call (2026-10-27)
            return TestModel(custom_output_text='{"side": "long", "score": 4}')
        return TestModel(custom_output_args={"side": "long", "score": 4})

    _seed_run(engine)
    router = _router(engine, factory=factory)
    trade = router.call("r1", "trader", "decide", Verdict)
    assert trade.ref.family == "deepseek"
    second = router.call("r1", "consensus", "check", Verdict, {"trader": trade.ref.family})
    assert second.ref.paid and second.ref.family == "openai"

    # With DeepSeek capped out the Trader lands on OpenAI. The Consensus must differ: DeepSeek is
    # blocked by its cap and OpenAI by the pairing, so nothing paid is left to try.
    _spend(engine, "c1", "deepseek", "deepseek-flash", 20_000_000, 0)  # $6.00
    first = router.call("r1", "trader", "decide", Verdict)
    assert first.ref.family == "openai"
    with pytest.raises(AllModelsFailedError):
        router.call("r1", "consensus", "check", Verdict, {"trader": first.ref.family})


def test_deepseek_at_its_cap_blocks_deepseek_but_not_openai(engine: Engine) -> None:
    router = _router(engine)
    assert _paid(router) == PAID_TAIL, "offered under the caps"
    # deepseek-flash at the recorded peak price: 10M in x $0.30 + 2M out x $1.20 = $5.40.
    _spend(engine, "c1", "deepseek", "deepseek-flash", 10_000_000, 2_000_000)
    assert router.month_to_date_spend_usd("deepseek") >= 5.0
    assert router.month_to_date_spend_usd("openai") == 0.0
    assert _paid(router) == PAID_TAIL[1:], "DeepSeek is out, OpenAI still answers"
    assert router.plan("trader"), "the free models are untouched by the cap"


def test_openai_at_its_cap_blocks_openai_but_not_deepseek(engine: Engine) -> None:
    router = _router(engine)
    # gpt-5-mini: 20M in x $0.25 = $5.00 on OpenAI alone reaches OpenAI's cap.
    _spend(engine, "c1", "openai", "gpt-5-mini", 20_000_000, 0)
    assert router.month_to_date_spend_usd("openai") == pytest.approx(5.0)
    assert router.month_to_date_spend_usd() == pytest.approx(5.0)
    assert _paid(router) == PAID_TAIL[:1], "OpenAI is out, DeepSeek still answers"


def test_spend_under_each_cap_can_still_trip_the_overall_ceiling(engine: Engine) -> None:
    """$3.90 + $4.00 is under both $5 caps, so only the $6 ceiling can stop it."""
    router = _router(engine, cap=6.0)
    _spend(engine, "c1", "deepseek", "deepseek-flash", 13_000_000, 0)  # $3.90
    _spend(engine, "c2", "openai", "gpt-5-mini", 16_000_000, 0)  # $4.00
    assert router.month_to_date_spend_usd("deepseek") < 5.0
    assert router.month_to_date_spend_usd("openai") < 5.0
    assert _paid(router) == []


def test_unknown_usage_is_charged_at_the_ceiling_to_its_own_provider(engine: Engine) -> None:
    router = _router(engine)
    # No token counts: 1M prompt characters in, plus a full max_tokens answer out (trader: 4000).
    _spend(engine, "c1", "deepseek", "deepseek-flash", None, None, prompt="x" * 1_000_000)
    expected = (1_000_000 * 0.30 + 4000 * 1.20) / 1_000_000
    assert router.month_to_date_spend_usd("deepseek") == pytest.approx(expected)
    assert router.month_to_date_spend_usd("openai") == 0.0


def test_an_unpriced_history_row_fails_closed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spend(engine, "c1", "deepseek", "deepseek-flash", 1, 1)
    config = copy.deepcopy(models_config())
    del config["prices"]["deepseek/deepseek-flash"]
    monkeypatch.setattr(router_module, "models_config", lambda: config)
    router = _router(engine)
    assert router.month_to_date_spend_usd("deepseek") == math.inf
    # DeepSeek's month is unknowable, so the overall ceiling (a sum over every provider) is
    # unknowable too: nothing paid is offered at all, OpenAI included.
    assert _paid(router) == []


class Verdict(BaseModel):
    side: str
    score: int


def test_the_paid_tail_is_reached_even_when_the_free_chain_exceeds_max_attempts(
    engine: Engine,
) -> None:
    """`max_attempts` (4) would cut a tail placed inside it; the paid slots sit outside it."""
    tried: list[str] = []

    def factory(ref: ModelRef) -> Any:
        tried.append(ref.label)
        if not ref.paid:
            raise RuntimeError("free model down")
        if ref.provider == "deepseek":  # prompted JSON, not a tool call (2026-10-27)
            return TestModel(custom_output_text='{"side": "long", "score": 4}')
        return TestModel(custom_output_args={"side": "long", "score": 4})

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES ('r1', 'manager', 'demo', '2026-09-20T00:00:00+00:00', 'RUNNING', 1, '-')"
            )
        )
    result = _router(engine, factory=factory).call("r1", "trader", "decide", Verdict)

    assert result.ref.label == "deepseek/deepseek-flash"
    assert [t for t in tried if not t.startswith("deepseek")] and len(tried) == 5
    assert result.attempts == 5


def test_openai_answers_when_every_free_model_and_deepseek_fail(engine: Engine) -> None:
    tried: list[str] = []

    def factory(ref: ModelRef) -> Any:
        tried.append(ref.label)
        if ref.provider != "openai":
            raise RuntimeError("down")
        if ref.provider == "deepseek":  # prompted JSON, not a tool call (2026-10-27)
            return TestModel(custom_output_text='{"side": "long", "score": 4}')
        return TestModel(custom_output_args={"side": "long", "score": 4})

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES ('r1', 'manager', 'demo', '2026-09-20T00:00:00+00:00', 'RUNNING', 1, '-')"
            )
        )
    result = _router(engine, factory=factory).call("r1", "trader", "decide", Verdict)
    assert result.ref.label == "openai/gpt-5-nano"
    assert tried[-2:] == PAID_TAIL, "DeepSeek is tried before OpenAI"


def test_a_days_worth_of_runs_fits_inside_the_free_quotas(engine: Engine) -> None:
    """Two manager runs a day over 15 assets is 30 calls per agent. The cheapest usable model in
    each role must absorb that alone — if the budget only works when every provider is healthy,
    it does not work."""
    calls_per_day = 2 * 15
    router = _router(engine)
    for agent in AGENTS:
        top = router.plan(agent)[0]
        _, rpd = limits_for(top)
        assert rpd >= calls_per_day * 2, (
            f"{agent}: {top.label} allows {rpd}/day, needs {calls_per_day} with headroom"
        )


def test_every_role_has_free_depth_behind_its_first_choice(engine: Engine) -> None:
    """Depth is what keeps the paid tier unreachable: one provider failing must not be enough."""
    router = _router(engine)
    for agent in AGENTS:
        free = [ref for ref in router.plan(agent) if not ref.paid]
        assert len(free) >= 5, f"{agent} has only {len(free)} free models"


def test_the_paid_tier_is_last_and_cheapest_first() -> None:
    order = [e.split("/", 1)[0] for e in models_config()["tiers"]["paid_fallback"]]
    assert order.index("deepseek") < order.index("openai")
    for tier in models_config()["tiers"]:
        if tier != "paid_fallback":
            assert all(not ref.paid for ref in tier_refs(tier)), tier


def test_every_agent_policy_points_at_a_free_tier() -> None:
    for agent in AGENTS:
        assert agent_policy(agent).tier != "paid_fallback"


def test_a_simulated_trading_day_never_reaches_the_paid_tier(engine: Engine) -> None:
    """Replay a day's traffic through the real quota bookkeeping.

    Two manager runs and two review runs over 15 assets, every agent called each time — 900
    calls — charged against the same counters production uses. The assertion is not that it is
    fast but that the last call of the day is still answered by a free model.
    """
    router = _router(engine)
    used: dict[str, str] = {}
    reached_paid: list[str] = []

    # Runs are hours apart and each asset takes seconds, so attempts are stamped with the
    # offset they would really have. Charging 900 calls to the same instant would only prove
    # that a 15-per-minute limit blocks 900 calls in one minute, which is not the question.
    for run in range(4):  # runs per day
        run_started = -(86_400 - run * 6 * 3600)
        for asset in range(15):  # assets per run
            for index, agent in enumerate(AGENTS):
                plan = router.plan(agent, used_families=used)
                assert plan, f"{agent} ran out of models in run {run}, asset {asset}"
                chosen = plan[0]
                if chosen.paid:
                    reached_paid.append(f"{agent}->{chosen.label}")
                used[agent] = chosen.family
                router.record_attempt(chosen, at_offset_s=run_started + asset * 30 + index)

    assert not reached_paid, f"paid models reached: {reached_paid[:5]}"


def test_the_day_leaves_headroom_rather_than_scraping_the_floor(engine: Engine) -> None:
    """Surviving the day is not enough: finishing it with every provider at its reserve would
    mean the next day starts with no fallback at all."""
    router = _router(engine)
    for run in range(4):
        run_started = -(86_400 - run * 6 * 3600)
        for asset in range(15):
            for index, agent in enumerate(AGENTS):
                plan = router.plan(agent)
                assert plan, f"{agent} exhausted in run {run}"
                router.record_attempt(plan[0], at_offset_s=run_started + asset * 30 + index)

    for agent in AGENTS:
        remaining = [
            ref for ref in router.plan(agent) if not ref.paid and router.headroom(ref) > 0.4
        ]
        assert len(remaining) >= 3, f"{agent} ends the day with {len(remaining)} comfortable models"


def test_one_runs_burst_fits_inside_the_per_minute_limits(engine: Engine) -> None:
    """The capacity ceiling nobody notices until all ten agents exist.

    A run is 15 assets. With one agent that is 15 calls; with ten it is 150, and the current run
    takes ~43s — so the burst lands inside a single minute. Aggregate RPM across the free
    providers has to cover that, or the router will be rotating into rate limits exactly when
    the run is busiest. Discovered by charging a day's calls to one instant (2026-09-20).
    """
    router = _router(engine)
    per_run = 15 * len(AGENTS)
    aggregate_rpm = sum(
        limits_for(ref)[0]
        for ref in {
            r.label: r for tier in ("fast", "strong_json", "reasoning") for r in tier_refs(tier)
        }.values()
    )
    assert aggregate_rpm >= per_run, (
        f"a single run needs {per_run} calls/min but the free providers total {aggregate_rpm}"
    )

    # And no single provider may be carrying an outsized share of that ceiling.
    served = [router.plan(agent)[0].provider for agent in AGENTS]
    busiest = max(served.count(p) for p in set(served))
    assert busiest <= 4, f"one provider leads {busiest} of {len(AGENTS)} roles: {served}"
