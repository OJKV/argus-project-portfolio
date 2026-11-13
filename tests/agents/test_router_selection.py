"""Which model gets asked, and why (LLM_AGENTS.md §5).

Selection is the part that decides whether a run costs nothing or costs money, so it is pure and
tested offline. Nothing here reaches a provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.breakers import BreakerRegistry
from argus.journal.db import open_journal
from argus.llm.router import Router, tier_refs


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "gemini_api_key": "g",
        "groq_api_key": "g",
        "openrouter_api_key": "g",
        "mistral_api_key": "g",
        "cloudflare_api_token": "g",
        "cloudflare_account_id": "acct",
        "zhipu_api_key": "g",
        "deepseek_api_key": "g",
        "openai_api_key": "g",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def _router(engine: Engine, **over: object) -> Router:
    return Router(engine=engine, settings=_settings(**over), breakers=BreakerRegistry())


def test_tier_refs_keep_config_order_and_carry_the_family() -> None:
    refs = tier_refs("fast")
    assert refs, "the fast tier must not be empty"
    assert refs[0].provider == "gemini", "flash-lite leads every tier (owner: 500 RPD)"
    assert all(ref.family for ref in refs)
    assert not any(ref.paid for ref in refs), "a free tier must never reach a paid provider"


def test_a_provider_without_a_key_is_skipped(engine: Engine) -> None:
    router = _router(engine, gemini_api_key="")
    assert not any(c.provider == "gemini" for c in router.candidates("fast"))


def test_cloudflare_needs_its_account_id_too(engine: Engine) -> None:
    router = _router(engine, cloudflare_account_id="")
    assert not any(c.provider == "cloudflare" for c in router.candidates("fast"))


def test_a_provider_near_its_daily_cap_is_skipped(engine: Engine) -> None:
    """Skipping at 90% leaves headroom; discovering the cap by being refused wastes the call."""
    router = _router(engine)
    assert any(c.provider == "gemini" for c in router.candidates("fast"))
    for _ in range(450):  # gemini rpd is 500 in models.yaml
        router.record_quota("gemini")
    assert not any(c.provider == "gemini" for c in router.candidates("fast"))


def test_an_open_breaker_removes_a_provider(engine: Engine) -> None:
    router = _router(engine)
    breaker = router.breakers.get("llm:groq")
    for _ in range(10):
        breaker.record_failure()
    assert not any(c.provider == "groq" for c in router.candidates("fast"))


def test_consensus_can_exclude_the_first_answer_s_family(engine: Engine) -> None:
    """A second opinion from the same family is not a second opinion."""
    router = _router(engine)
    first = router.candidates("strong_json")[0]
    second = router.candidates("strong_json", exclude_families={first.family})
    assert second, "a disagreeing family must remain available"
    assert all(c.family != first.family for c in second)


def test_paid_fallback_is_only_offered_when_asked_for(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import copy

    import argus.llm.router as router_module
    from argus.config import models_config

    # Priced for the test: an unpriced paid model is never offered at all (L-7,
    # tests/agents/test_paid_cap.py). This test is about ordering, not money.
    config = copy.deepcopy(models_config())
    config["prices"] = {
        entry: {"in_per_mtok": 1.0, "out_per_mtok": 1.0}
        for entry in config["tiers"]["paid_fallback"]
    }
    monkeypatch.setattr(router_module, "models_config", lambda: config)

    router = _router(engine)
    assert all(not c.paid for c in router.candidates("strong_json"))
    paid = router.candidates("paid_fallback")
    assert paid and all(c.paid for c in paid)
    assert paid[0].provider == "deepseek", "cents before dollars"


def test_quota_is_counted_per_provider_per_day(engine: Engine) -> None:
    router = _router(engine)
    router.record_quota("groq")
    router.record_quota("groq")
    router.record_quota("gemini")
    assert router.quota_used("groq") == 2
    assert router.quota_used("gemini") == 1
    assert router.quota_used("mistral") == 0


def test_listing_candidates_does_not_burn_a_half_open_probe(engine: Engine) -> None:
    """Listing is a peek; only the real request may take the single half-open probe."""
    router = _router(engine)
    breaker = router.breakers.get("llm:groq")
    now = [0.0]
    breaker._clock = lambda: now[0]
    for _ in range(breaker.threshold):
        breaker.record_failure()
    now[0] += breaker.cooldown_s
    for _ in range(3):
        assert any(c.provider == "groq" for c in router.candidates("fast"))
    assert breaker.allow() is True, "the listing consumed the probe"
