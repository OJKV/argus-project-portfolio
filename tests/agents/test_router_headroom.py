"""Route around a limit before it is reached, not after (owner, 2026-09-19).

A 429 costs the call, its latency and a slot in the run budget, and on a free tier the whole point
of having a second provider is to not need the first one's refusal to find out. So a provider's
remaining headroom changes its *position* well before it changes its availability.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.breakers import BreakerRegistry
from argus.journal.db import open_journal
from argus.llm.router import COMFORTABLE, RESERVE, Router, limits_for, tier_refs


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _router(engine: Engine) -> Router:
    settings = Settings(
        _env_file=None,
        gemini_api_key="g",
        groq_api_key="g",
        openrouter_api_key="g",
        mistral_api_key="g",
        cloudflare_api_token="g",
        cloudflare_account_id="acct",
        zhipu_api_key="g",
        deepseek_api_key="g",
        openai_api_key="g",
    )
    return Router(engine=engine, settings=settings, breakers=BreakerRegistry())


def test_limits_fall_back_from_model_to_provider() -> None:
    gemini = next(r for r in tier_refs("fast") if r.provider == "gemini")
    rpm, rpd = limits_for(gemini)
    assert rpd == 500, "gemini flash-lite free tier is 500 RPD (verified 2026-09-19)"
    assert rpm > 0


def test_with_everything_idle_the_order_is_the_configured_one(engine: Engine) -> None:
    router = _router(engine)
    assert [c.label for c in router.candidates("fast")] == [r.label for r in tier_refs("fast")]


def test_a_provider_running_low_is_demoted_but_still_offered(engine: Engine) -> None:
    router = _router(engine)
    first = tier_refs("fast")[0]
    _, rpd = limits_for(first)
    # Spend enough that the headroom LEFT sits between RESERVE and COMFORTABLE — the band
    # where a provider is still usable but should no longer lead.
    used = int(rpd * (1 - (COMFORTABLE + RESERVE) / 2))
    for _ in range(used):
        router.record_quota(first.provider)

    order = [c.provider for c in router.candidates("fast")]
    assert first.provider in order, "still usable — demoting is not blocking"
    assert order[0] != first.provider, "a provider close to its cap should not lead the queue"


def test_a_provider_past_the_reserve_is_dropped(engine: Engine) -> None:
    router = _router(engine)
    first = tier_refs("fast")[0]
    _, rpd = limits_for(first)
    for _ in range(int(rpd * (1 - RESERVE)) + 1):
        router.record_quota(first.provider)
    assert first.provider not in [c.provider for c in router.candidates("fast")]


def test_the_per_minute_window_blocks_a_burst_before_the_provider_does(engine: Engine) -> None:
    """RPD is the slow limit; RPM is the one a 15-asset run trips inside a single minute."""
    router = _router(engine)
    ref = tier_refs("fast")[0]
    rpm, _ = limits_for(ref)
    for _ in range(rpm):
        router.record_attempt(ref)
    assert ref.provider not in [c.provider for c in router.candidates("fast")]


def test_a_minute_old_burst_no_longer_counts(engine: Engine) -> None:
    router = _router(engine)
    ref = tier_refs("fast")[0]
    rpm, _ = limits_for(ref)
    for _ in range(rpm):
        router.record_attempt(ref, at_offset_s=-120)  # two minutes ago
    assert ref.provider in [c.provider for c in router.candidates("fast")]
