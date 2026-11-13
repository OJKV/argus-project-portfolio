"""One model must not play every role (owner, 2026-09-19).

Two independent reasons, and both are about evidence rather than cost:

* **Bias.** An Analyst and a Red Team from the same family share training data and blind spots, so
  the Red Team validates the Analyst's mistakes instead of finding them. Agreement between
  siblings is not evidence.
* **Concentration.** Ten agents x 15 assets through one provider is a rate limit with extra steps,
  and one provider outage becomes a dead run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings, models_config
from argus.exchange.breakers import BreakerRegistry
from argus.journal.db import open_journal
from argus.llm.router import ADVERSARIAL_PAIRS, Router, agent_policy, tier_refs

AGENTS = (
    "regime",
    "analyst",
    "redteam",
    "riskdebate",
    "trader",
    "consensus",
    "reviewer",
    "postmortem",
    "reflector",
    "catalyst",
)


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


def test_every_agent_has_a_policy() -> None:
    for agent in AGENTS:
        policy = agent_policy(agent)
        assert policy.tier in models_config()["tiers"]
        assert policy.families, f"{agent} must name at least one preferred family"


def test_preferred_families_exist_in_the_agent_s_tier() -> None:
    """A preference for a family the tier does not contain is a silent no-op."""
    for agent in AGENTS:
        policy = agent_policy(agent)
        available = {ref.family for ref in tier_refs(policy.tier)}
        assert set(policy.families) <= available, f"{agent}: {policy.families} vs {available}"


def test_the_preferred_family_leads_the_queue(engine: Engine) -> None:
    router = _router(engine)
    for agent in AGENTS:
        policy = agent_policy(agent)
        plan = router.plan(agent)
        assert plan, f"{agent} has no usable model"
        assert plan[0].family == policy.families[0]


def test_an_adversarial_agent_never_shares_a_family_with_its_counterpart(engine: Engine) -> None:
    router = _router(engine)
    for agent, counterpart in ADVERSARIAL_PAIRS.items():
        used = router.plan(counterpart)[0]
        plan = router.plan(agent, used_families={counterpart: used.family})
        assert plan, f"{agent} has no model outside {used.family}"
        assert all(ref.family != used.family for ref in plan)


def test_the_roster_spreads_across_providers(engine: Engine) -> None:
    """If one provider served every role, its rate limit would be the run's rate limit."""
    router = _router(engine)
    chosen = {agent: router.plan(agent)[0].family for agent in AGENTS}
    assert len(set(chosen.values())) >= 4, chosen
    busiest = max(sum(1 for f in chosen.values() if f == fam) for fam in set(chosen.values()))
    # Four, not three, since 2026-09-24. owner's rule is "no single provider may lead more than 4
    # of the 10 roles"; this test was written at 3 when the map happened to allow it, and holding
    # it there would have kept Regime on a family that cannot read its own market block — every
    # ministral size answers "no contradictory signals" with a 97th-percentile funding reading in
    # the prompt. A guard should encode the rule, not the most convenient map that ever passed.
    assert busiest <= 4, f"one family carries too much of the roster: {chosen}"


def test_preference_yields_to_availability(engine: Engine) -> None:
    """Purity is a preference, not a requirement: a run must still happen when a family is down."""
    router = _router(engine)
    policy = agent_policy("analyst")
    plan = router.plan("analyst", used_families={"__blocked__": policy.families[0]})
    assert plan, "blocking the preferred family must not empty the queue"


def test_a_blocked_family_never_stops_the_run(engine: Engine) -> None:
    """owner's rule: the trading engine keeps going. Family separation is a quality preference, not
    a licence to return nothing — a Red Team from the Analyst's own family is worth more than no
    Red Team at all, as long as the degradation is visible.
    """
    router = _router(engine)
    for agent, counterpart in ADVERSARIAL_PAIRS.items():
        policy = agent_policy(agent)
        families = {ref.family for ref in tier_refs(policy.tier)}
        for family in families:
            plan = router.plan(agent, used_families={counterpart: family})
            assert plan, f"{agent}: blocking {family} emptied the queue"


def test_a_role_that_can_keep_its_distance_still_does(engine: Engine) -> None:
    router = _router(engine)
    for agent, counterpart in ADVERSARIAL_PAIRS.items():
        used = router.plan(counterpart)[0].family
        plan = router.plan(agent, used_families={counterpart: used})
        assert plan[0].family != used, f"{agent} should avoid {used} while it can"


def test_every_role_has_real_depth(engine: Engine) -> None:
    """3+ usable models per role, or one provider's bad afternoon is a dead agent."""
    router = _router(engine)
    for agent in AGENTS:
        plan = router.plan(agent)
        assert len(plan) >= 3, f"{agent} has only {len(plan)} models: {[r.label for r in plan]}"
        assert len({r.family for r in plan}) >= 2, f"{agent} is concentrated in one family"


def test_separation_yields_when_it_is_the_last_model_standing(engine: Engine) -> None:
    """The hardest case: every other family is down and the only model left shares the
    counterpart's family. Returning nothing would stop the engine over a quality preference.
    """
    router = _router(engine)
    agent, counterpart = next(iter(ADVERSARIAL_PAIRS.items()))
    tier = agent_policy(agent).tier
    survivor = tier_refs(tier)[0].family
    for ref in tier_refs(tier):
        if ref.family != survivor:
            breaker = router.breakers.get(ref.breaker_key)
            for _ in range(10):
                breaker.record_failure()

    plan = router.plan(agent, used_families={counterpart: survivor})
    assert plan, "a degraded second opinion beats no second opinion"
    # The paid last resort trails the free chain and is not part of what separation yields to.
    assert all(ref.family == survivor for ref in plan if not ref.paid)
