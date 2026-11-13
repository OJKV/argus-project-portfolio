"""config/models.yaml against provider facts verified 2026-09-17."""

from __future__ import annotations

from argus.config import models_config
from argus.doctor import KEY_CHECKS, OPTIONAL_KEYS, check_keys

RETIRED_PROVIDERS = {"github"}  # GitHub Models fully retired 2026-07-30


def _entries() -> list[str]:
    return [e for tier in models_config()["tiers"].values() for e in tier]


def test_no_retired_provider_is_configured() -> None:
    cfg = models_config()
    assert not RETIRED_PROVIDERS & set(cfg["providers"])
    assert not [e for e in _entries() if e.split("/", 1)[0] in RETIRED_PROVIDERS]


def test_every_tier_entry_names_a_configured_provider() -> None:
    providers = set(models_config()["providers"])
    assert {e.split("/", 1)[0] for e in _entries()} <= providers


def test_zhipu_uses_the_international_platform_and_a_free_model() -> None:
    # open.bigmodel.cn needs a mainland-China phone number; api.z.ai does not.
    cfg = models_config()
    assert cfg["providers"]["zhipu"]["base_url"] == "https://api.z.ai/api/paas/v4"
    assert any(e.startswith("zhipu/") and "flash" in e for e in _entries())


def test_paid_providers_only_appear_in_the_paid_fallback_tier() -> None:
    """A paid model reached from a free tier is a bill nobody authorised."""
    cfg = models_config()
    paid = {name for name, p in cfg["providers"].items() if p.get("paid")}
    for tier, entries in cfg["tiers"].items():
        used = {e.split("/", 1)[0] for e in entries}
        if tier == "paid_fallback":
            assert used <= paid, f"{tier} must contain only paid providers"
        else:
            assert not (used & paid), f"free tier {tier} reaches a paid provider"


MIXED_PROVIDERS = {"openrouter", "zhipu"}  # sell free and paid models under one key


def test_providers_that_sell_paid_models_name_their_free_ones() -> None:
    """Without an allowlist, a paid model on a mixed provider looks free and spends uncapped."""
    providers = models_config()["providers"]
    for name in MIXED_PROVIDERS:
        assert providers[name].get("free_models"), f"{name} must list its free models"


def test_paid_is_judged_per_model_on_a_mixed_provider() -> None:
    from argus.llm.router import is_paid

    assert is_paid("zhipu", "glm-5.3-flash"), "Zhipu's cheapest paid model (measured 2026-09-20)"
    assert is_paid("openrouter", "nex-agi/nex-n2.5-mini"), "an OpenRouter ID without :free"
    assert not is_paid("zhipu", "glm-4.7-flash")
    assert not is_paid("openrouter", "cohere/north-mini-code:free")
    assert is_paid("deepseek", "deepseek-flash")


def test_no_free_tier_holds_a_paid_model_and_the_paid_tier_holds_nothing_else() -> None:
    from argus.llm.router import tier_refs

    for tier in models_config()["tiers"]:
        refs = tier_refs(tier)
        if tier == "paid_fallback":
            assert all(ref.paid for ref in refs), tier
        else:
            assert not [ref.label for ref in refs if ref.paid], f"free tier {tier} is paid"


def test_paid_fallback_is_ordered_cheapest_first() -> None:
    # owner's rule: DeepSeek (cents) before OpenAI (dollars), and only after every free provider.
    order = [e.split("/", 1)[0] for e in models_config()["tiers"]["paid_fallback"]]
    assert order.index("deepseek") < order.index("openai")


def test_every_free_tier_has_more_than_one_provider() -> None:
    """One provider per tier means one outage is a dead run."""
    cfg = models_config()
    for tier, entries in cfg["tiers"].items():
        if tier == "paid_fallback":
            continue
        assert len({e.split("/", 1)[0] for e in entries}) >= 3, f"{tier} is too concentrated"


def test_consensus_still_has_an_openai_family_model_after_github_retired() -> None:
    assert any("openai/gpt-oss" in e for e in models_config()["tiers"]["strong_json"])


def test_doctor_no_longer_asks_for_a_github_token() -> None:
    assert "github_token" not in KEY_CHECKS.values()


def test_optional_keys_do_not_fail_doctor_when_unset() -> None:
    checks = {c.name: c for c in check_keys()}
    for label in OPTIONAL_KEYS:
        assert checks[label].ok, f"{label} is optional and must not fail doctor"
        assert "optional" in checks[label].detail


def test_strong_json_holds_no_model_that_cannot_compile_a_nested_schema() -> None:
    """Cloudflare's mistral-small-3.1 rejects every schema with $defs ("Grammar error: Pointer
    '/$defs/TradePlan' does not exist"): 15 of 15 Consensus calls failed 2026-11-01..11 (F-168).
    Every strong_json schema nests models, so the entry was a wasted call per run."""
    assert (
        "cloudflare/@cf/mistralai/mistral-small-3.1-24b-instruct"
        not in models_config()["tiers"]["strong_json"]
    )
