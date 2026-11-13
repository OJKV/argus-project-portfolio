"""Model selection and call logging (LLM_AGENTS.md §5).

Two jobs, kept apart on purpose:

* **Selection** is pure and offline — which model to ask, in what order, and which to skip because
  its key is missing, its daily quota is nearly spent, or its circuit breaker is open. This is the
  part that decides whether a run costs nothing or costs money, so it is tested without a network.
* **Execution** is a thin wrapper around Pydantic AI that tries candidates in order and journals
  every attempt to `llm_calls`.

Model IDs live in `config/models.yaml`, never in code: free tiers rotate, and pinning them here is
how the whole catalogue quietly died once already (F-063). No LiteLLM (CLAUDE.md rule 7).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

import structlog
from sqlalchemy import Engine, bindparam, text

from argus.config import Settings, get_settings, models_config
from argus.exchange.breakers import BreakerRegistry
from argus.journal.db import new_id, utc_now

log = structlog.get_logger(__name__)

# Headroom bands. A provider is never allowed to run to its limit: with less than RESERVE of its
# quota left it stops being offered at all, and from COMFORTABLE downwards it is demoted behind
# providers with more room. Routing *before* the refusal is the point — a 429 costs the call, its
# latency and a slot in the run budget, and on a free tier the next provider exists precisely so
# the first one's refusal is never needed (owner, 2026-09-19).
RESERVE = 0.1  # less than 10% left -> not offered
COMFORTABLE = 0.4  # less than 40% left -> still offered, but after roomier providers
# Paid models one call may try after the free chain, for an agent with `paid_last_resort`: the
# whole priced tail (DeepSeek first, then OpenAI), so one provider's outage cannot use them all up.
PAID_LAST_RESORT_ATTEMPTS = 4
RPM_WINDOW_S = 60
# How long to stop offering a provider that just refused us. Our counters only know about calls
# WE made; a provider exhausted by another project, or by yesterday's testing, looks fresh to
# them. A 429 is the provider telling us the truth, and it should not have to repeat itself
# fifteen times in one run (measured 2026-09-20).
RATE_LIMIT_COOLDOWN_S = 90.0
DAILY_EXHAUSTED_COOLDOWN_S = 6 * 3600.0
_DAILY_MARKERS = ("daily", "per day", "quota exceeded", "allocation")


def cooldown_seconds(message: str) -> float:
    """A busy minute and a spent daily allowance deserve different patience."""
    text = message.lower()
    if any(marker in text for marker in _DAILY_MARKERS):
        return DAILY_EXHAUSTED_COOLDOWN_S
    return RATE_LIMIT_COOLDOWN_S


@dataclass(frozen=True)
class ModelRef:
    """One entry of one tier, with everything selection needs to judge it."""

    provider: str
    model: str
    family: str
    env: str
    paid: bool
    rpm: int
    rpd: int
    # Tokens per minute the provider admits for one request, 0 = not measured. Groq meters tokens,
    # not requests (F-113): a prompt larger than this is refused with 413 whatever the RPM says.
    tpm: int = 0

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def breaker_key(self) -> str:
        return f"llm:{self.provider}"


def _split(entry: str) -> tuple[str, str]:
    """`provider/model` where the model may itself contain slashes (`groq/openai/gpt-oss-120b`)."""
    provider, _, model = entry.partition("/")
    return provider, model


def limits_for(ref: ModelRef) -> tuple[int, int]:
    """(rpm, rpd) for one model: its own published limit if there is one, else the provider's.

    Groq and Google ration per model, OpenRouter per account — so a provider-level number is the
    fallback, not the truth. An unknown limit stays at the provider default rather than being
    guessed high: too high means finding the cap by being refused, which is the thing to avoid.
    """
    override = (models_config().get("limits") or {}).get(ref.label) or {}
    return int(override.get("rpm", ref.rpm)), int(override.get("rpd", ref.rpd))


def price_per_mtok(label: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens for one model, or None when nobody has priced it.

    Only paid models need an entry. None is not "free": the router refuses to offer a paid model
    it cannot price, because a monthly cap that cannot count a call is not a cap (L-7).
    """
    entry = (models_config().get("prices") or {}).get(label)
    if not entry:
        return None
    return float(entry["in_per_mtok"]), float(entry["out_per_mtok"])


def is_paid(provider: str, model: str) -> bool:
    """Whether one model costs money: a property of the model, not only of its provider (L-7).

    A `paid: true` provider bills every model. A provider that sells free and paid models under
    one key (OpenRouter, Zhipu) lists its free ones as `free_models` patterns, and any model not
    matched is paid — so a new ID is capped and priced until someone shows it is free. An
    unknown provider is paid for the same reason.
    """
    spec = (models_config().get("providers") or {}).get(provider)
    if spec is None or spec.get("paid"):
        return True
    free = spec.get("free_models")
    return free is not None and not any(fnmatchcase(model, str(p)) for p in free)


def tier_refs(tier: str) -> list[ModelRef]:
    config = models_config()
    providers: dict[str, dict[str, Any]] = config.get("providers", {})
    refs: list[ModelRef] = []
    for entry in config.get("tiers", {}).get(tier, []):
        provider, model = _split(str(entry))
        spec = providers.get(provider)
        if spec is None:
            log.warning("router.unknown_provider", provider=provider, tier=tier)
            continue
        refs.append(
            ModelRef(
                provider=provider,
                model=model,
                family=str(spec.get("family", provider)),
                env=str(spec["env"]),
                paid=is_paid(provider, model),
                rpm=int(spec.get("rpm", 0)),
                rpd=int(spec.get("rpd", 0)),
                tpm=int(spec.get("tpm", 0)),
            )
        )
    return refs


def _output_spec(ref: ModelRef, output_type: Any) -> Any:
    """How a provider is asked for structured output: a forced tool call, or JSON in the prompt.

    Groq's models cannot be forced into a tool call: qwen3.8-27b answers 400 "Failed to call a
    function" and gpt-oss-120b "Tool choice is required", so every structured call fell through
    to the next provider after a timeout (Logfire, 2026-10-26). A provider marked
    `output_mode: prompted` in models.yaml gets the schema in the prompt and its text answer
    parsed and validated against the same type, so nothing unvalidated reaches a caller (rule 6).
    """
    from pydantic_ai import PromptedOutput

    spec = (models_config().get("providers") or {}).get(ref.provider) or {}
    return PromptedOutput(output_type) if spec.get("output_mode") == "prompted" else output_type


def approx_tokens(text: str) -> int:
    """A rough token count (~4 characters a token). Only used to rule out a provider that a
    prompt cannot possibly fit; the margins it guards are 2x, not 5%."""
    return len(text) // 4


class AllModelsFailedError(RuntimeError):
    """Every candidate for an agent failed. The caller decides whether that is fatal."""


@dataclass(frozen=True)
class CallResult[T]:
    output: T
    ref: ModelRef
    attempts: int
    call_id: str
    repaired: bool


def build_model(ref: ModelRef, settings: Settings | None = None) -> Any:
    """A Pydantic AI model for one entry: native for Gemini and Mistral, OpenAI-compatible else.

    Every provider we use speaks one of those three dialects. No LiteLLM (CLAUDE.md rule 7).
    """
    from openai import AsyncOpenAI
    from pydantic_ai.models.google import GoogleModel
    from pydantic_ai.models.mistral import MistralModel
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.google import GoogleProvider
    from pydantic_ai.providers.mistral import MistralProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    s = settings or get_settings()
    api_key = getattr(s, ref.env.lower()).get_secret_value()
    if ref.provider == "gemini":
        return GoogleModel(ref.model, provider=GoogleProvider(api_key=api_key))
    if ref.provider == "mistral":
        return MistralModel(ref.model, provider=MistralProvider(api_key=api_key))
    base_url = str(models_config()["providers"][ref.provider]["base_url"])
    if "{account}" in base_url:
        base_url = base_url.format(account=s.cloudflare_account_id)
    # max_retries=0: the OpenAI SDK otherwise retries 408/409/429/5xx and timeouts twice inside
    # one run_sync — three requests per counted attempt, a 429 hidden from the cooldown, and a
    # paid timeout billed three times for one charged ceiling (L-6, L-7). The router retries.
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0)
    return OpenAIChatModel(ref.model, provider=OpenAIProvider(openai_client=client))


# Roles that judge another role's output, from config. Kept as a module constant so a test can
# assert the whole map rather than one pair at a time.
ADVERSARIAL_PAIRS: dict[str, str] = dict(models_config().get("adversarial") or {})


class Criticality(StrEnum):
    """What an agent's failure means for the run.

    Three outcomes, not two. "Optional" is not the opposite of "critical": an agent whose job is
    to *object* cannot be skipped when it fails, because skipping it silently turns a safety
    check into an approval.
    """

    REQUIRED = "required"  # no answer, no signal
    SAFETY = "safety"  # the check did not run, so the trade does not happen
    OPTIONAL = "optional"  # the run continues; only the log notices


@dataclass(frozen=True)
class AgentPolicy:
    """Which tier an agent draws from, and which families it prefers within it."""

    agent: str
    tier: str
    families: tuple[str, ...]
    counterpart: str | None
    criticality: Criticality
    # Opt-in (owner, 2026-10-26): after the whole free chain, the priced `paid_fallback` models.
    # On for every agent; still subject to the price entry and the per-provider monthly caps.
    paid_last_resort: bool = False


def agent_policy(agent: str) -> AgentPolicy:
    spec = (models_config().get("agents") or {}).get(agent)
    if spec is None:
        raise KeyError(f"no model policy for agent {agent!r} — add one to config/models.yaml")
    return AgentPolicy(
        agent=agent,
        tier=str(spec["tier"]),
        families=tuple(str(f) for f in spec.get("families") or ()),
        counterpart=ADVERSARIAL_PAIRS.get(agent),
        criticality=Criticality(str(spec.get("criticality", "optional"))),
        paid_last_resort=bool(spec.get("paid_last_resort", False)),
    )


@dataclass
class Router:
    engine: Engine
    settings: Settings = field(default_factory=get_settings)
    breakers: BreakerRegistry = field(default_factory=BreakerRegistry)
    # Injectable so the offline suite exercises the real control flow with test doubles.
    model_factory: Callable[[ModelRef], Any] = build_model
    # Wall-clock ceiling for one agent's answer, attempts notwithstanding. Four models x a
    # 45s timeout, each with a repair, is minutes per asset; across 15 assets that is the
    # whole run budget. No role waits longer than this (owner, 2026-09-20).
    deadline_s: float = 600.0
    # provider -> monotonic time until which it is not offered. Process-local on purpose: a
    # run is one short-lived process working through the universe, which is exactly the
    # window where repeating a known-dead provider costs the most.
    _cooldowns: dict[str, float] = field(default_factory=dict)

    # --- quota ------------------------------------------------------------
    def _today(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def quota_used(self, provider: str) -> int:
        with self.engine.connect() as conn:
            used = conn.execute(
                text("SELECT calls FROM llm_quota WHERE provider = :p AND day = :d"),
                {"p": provider, "d": self._today()},
            ).scalar()
        return int(used or 0)

    def record_quota(self, provider: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_quota (provider, day, calls) VALUES (:p, :d, 1)"
                    " ON CONFLICT(provider, day) DO UPDATE SET calls = calls + 1"
                ),
                {"p": provider, "d": self._today()},
            )

    # --- selection --------------------------------------------------------
    def record_attempt(self, ref: ModelRef, at_offset_s: float = 0.0) -> None:
        """Note a call against the per-minute window. `at_offset_s` is for tests only."""
        moment = datetime.now(UTC) + timedelta(seconds=at_offset_s)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_attempts (attempt_id, provider, model, created_at)"
                    " VALUES (:i, :p, :m, :t)"
                ),
                {"i": new_id(), "p": ref.provider, "m": ref.model, "t": moment.isoformat()},
            )
        self.record_quota(ref.provider)

    def calls_last_minute(self, ref: ModelRef) -> int:
        cutoff = (datetime.now(UTC) - timedelta(seconds=RPM_WINDOW_S)).isoformat()
        with self.engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM llm_attempts WHERE provider = :p AND created_at >= :c"),
                {"p": ref.provider, "c": cutoff},
            ).scalar()
        return int(count or 0)

    def headroom(self, ref: ModelRef) -> float:
        """Fraction of the tighter of the two quotas still unused, 0.0 to 1.0."""
        rpm, rpd = limits_for(ref)
        fractions = [1.0]
        if rpm:
            fractions.append(1.0 - self.calls_last_minute(ref) / rpm)
        if rpd:
            fractions.append(1.0 - self.quota_used(ref.provider) / rpd)
        return max(0.0, min(fractions))

    def record_rate_limit(self, ref: ModelRef, seconds: float) -> None:
        until = time.monotonic() + seconds
        self._cooldowns[ref.provider] = max(self._cooldowns.get(ref.provider, 0.0), until)
        log.info("router.cooldown", provider=ref.provider, seconds=round(seconds))

    # --- money ------------------------------------------------------------
    def month_to_date_spend_by_provider_usd(self) -> dict[str, float]:
        """What paid models have cost this UTC calendar month, per provider, from `llm_calls`.

        An attempt with no reported usage (a timeout, an error after the provider started
        generating) is charged at its ceiling: one token per prompt character and a full
        `max_tokens` answer. Over-counting reaches the cap early, which is the direction a cap
        should fail in. A paid call on a model with no price makes that provider's month
        unknowable, and an unknowable spend is treated as unlimited (inf). Paid is judged per
        model (`is_paid`), so a paid model on a provider that also sells free ones is counted too.
        """
        paid = sorted(
            n
            for n, spec in (models_config().get("providers") or {}).items()
            if spec.get("paid") or "free_models" in spec
        )
        if not paid:
            return {}
        month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        query = text(
            "SELECT provider, model, agent, tokens_in, tokens_out, LENGTH(prompt) AS chars"
            " FROM llm_calls WHERE created_at >= :month AND provider IN :paid"
        ).bindparams(bindparam("paid", expanding=True))
        with self.engine.connect() as conn:
            rows = conn.execute(query, {"month": month.isoformat(), "paid": paid}).all()
        spent: dict[str, float] = {}
        for row in rows:
            if not is_paid(row.provider, row.model):
                continue
            price = price_per_mtok(f"{row.provider}/{row.model}")
            if price is None:
                spent[row.provider] = math.inf
                continue
            tokens_in = row.tokens_in if row.tokens_in is not None else int(row.chars or 0)
            tokens_out = (
                row.tokens_out
                if row.tokens_out is not None
                else self.model_settings(row.agent, row.provider)["max_tokens"]
            )
            spent[row.provider] = (
                spent.get(row.provider, 0.0)
                + (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
            )
        return spent

    def month_to_date_spend_usd(self, provider: str | None = None) -> float:
        """This month's paid spend: one provider's, or (no argument) the sum over all of them."""
        spent = self.month_to_date_spend_by_provider_usd()
        if provider is not None:
            return spent.get(provider, 0.0)
        return sum(spent.values())

    def _paid_allowed(self, ref: ModelRef) -> bool:
        """A paid model is offered only while spend is under its provider's cap AND the overall
        ceiling (L-7).

        Checked in selection, not after the call: the point of a cap is the call it prevents.
        A provider with no entry in `paid_monthly_cap_usd_by_provider` has a cap of zero, and
        an unpriced model or an unpriced history row fails closed.
        """
        if price_per_mtok(ref.label) is None:
            log.warning("router.paid_unpriced", model=ref.label)
            return False
        by_provider = self.month_to_date_spend_by_provider_usd()
        spent = by_provider.get(ref.provider, 0.0)
        cap = self.settings.paid_monthly_cap_usd_by_provider.get(ref.provider, 0.0)
        if spent >= cap:
            log.warning(
                "router.paid_cap_reached",
                model=ref.label,
                provider=ref.provider,
                spent_usd=spent,
                cap_usd=cap,
            )
            return False
        total = sum(by_provider.values())
        ceiling = self.settings.paid_llm_monthly_cap_usd
        if total >= ceiling:
            log.warning(
                "router.paid_ceiling_reached", model=ref.label, spent_usd=total, cap_usd=ceiling
            )
            return False
        return True

    def _available(self, ref: ModelRef) -> bool:
        if self._cooldowns.get(ref.provider, 0.0) > time.monotonic():
            return False
        if not self.settings.has(ref.env.lower()):
            return False
        if ref.provider == "cloudflare" and not self.settings.cloudflare_account_id:
            return False
        if ref.paid and not self._paid_allowed(ref):
            return False
        # A peek: this filter lists refs that may never be called (the probe slot is taken
        # by `allow()` at the request itself).
        return self.breakers.get(ref.breaker_key).would_allow()

    def candidates(self, tier: str, exclude_families: Collection[str] = ()) -> list[ModelRef]:
        """The tier's models, ordered by how much room they have left.

        Config order is preserved among providers with room to spare; one running low is demoted
        rather than dropped, and only a provider inside its RESERVE is withheld entirely.
        `exclude_families` is what makes a consensus call a second *opinion*: asking the same
        family twice mostly reproduces the same answer in different words.
        """
        excluded = set(exclude_families)
        scored: list[tuple[int, int, ModelRef]] = []
        for position, ref in enumerate(tier_refs(tier)):
            if ref.family in excluded or not self._available(ref):
                continue
            room = self.headroom(ref)
            if room <= RESERVE:
                continue
            # Two bands only: finer sorting would reshuffle the quality order in models.yaml
            # every time a counter ticked.
            band = 0 if room >= COMFORTABLE else 1
            scored.append((band, position, ref))
        return [ref for _, _, ref in sorted(scored, key=lambda row: (row[0], row[1]))]

    def plan(self, agent: str, used_families: Mapping[str, str] | None = None) -> list[ModelRef]:
        """The models to try for one agent, best first.

        Preferred families lead, in the order the config lists them; everything else in the tier
        follows as a fallback, because a run that cannot happen is worse than one answered by a
        second-choice family. If this agent judges another agent's output, the family that
        produced that output is removed entirely — a reviewer from the same lab as the trader is
        an echo, not a review.
        """
        policy = agent_policy(agent)
        excluded: set[str] = set()
        if policy.counterpart and used_families:
            counterpart_family = used_families.get(policy.counterpart)
            if counterpart_family:
                excluded.add(counterpart_family)
        # A family named under any key blocks it — lets callers force a spread explicitly.
        if used_families:
            excluded.update(
                family
                for key, family in used_families.items()
                if key.startswith("__") and key.endswith("__")
            )
        ranked = self.candidates(policy.tier, exclude_families=excluded)
        if not ranked and excluded:
            # The engine keeps going (owner, 2026-09-20). Family separation is a quality preference,
            # not a licence to return nothing: a Red Team from the Analyst's own family is worth
            # more than no Red Team at all. The degradation is logged so it is never silent.
            log.warning(
                "router.separation_degraded",
                agent=agent,
                blocked=sorted(excluded),
                reason="no model outside the counterpart's family is available",
            )
            ranked = self.candidates(policy.tier)
        preference = {family: rank for rank, family in enumerate(policy.families)}
        chain = sorted(ranked, key=lambda ref: preference.get(ref.family, len(preference)))
        if policy.paid_last_resort:
            # After the whole free chain, cheapest first (config order). `_available` has already
            # dropped any paid model that is unpriced or past the monthly cap (L-7).
            chain += self.candidates("paid_fallback", exclude_families=excluded)
        return chain

    # --- execution --------------------------------------------------------
    def _temperature(self, agent: str) -> float:
        policy = models_config().get("policy", {}).get("temperature", {})
        return float(policy.get(agent, policy.get("default", 0.3)))

    def model_settings(self, agent: str, provider: str | None = None) -> dict[str, Any]:
        """Per-call settings. `max_tokens` is never left to the provider.

        Cloudflare's gpt-oss-120b answered "token limit (provider default) exceeded before any
        response was generated": a reasoning model spends an unset budget on thinking and returns
        nothing, which looks like a broken model rather than a missing setting.
        """
        policy = models_config().get("policy", {})
        ceiling = int(policy.get("max_tokens", 2000))
        # A thinking model spends the output budget on reasoning before it answers: cohere
        # stopped at 2000 and gemini-3.5-flash at 1920 on the Trader (Logfire, 2026-10-26).
        ceiling = int((policy.get("max_tokens_by_agent") or {}).get(agent, ceiling))
        by_provider = policy.get("max_tokens_by_provider") or {}
        if provider and provider in by_provider:
            # Groq rejects a request whose max_tokens alone exceeds its per-minute output limit,
            # so the ceiling has to be lowered before the call, not after the 429.
            ceiling = min(ceiling, int(by_provider[provider]))
        floors = policy.get("min_tokens_by_provider") or {}
        if provider and provider in floors:
            # A reasoning model spends the budget thinking first (OpenAI gpt-5, 2026-10-27).
            ceiling = max(ceiling, int(floors[provider]))
        return {
            "temperature": self._temperature(agent),
            "timeout": float(policy.get("timeout_s", 45)),
            "max_tokens": ceiling,
        }

    def call[T](
        self,
        run_id: str,
        agent: str,
        prompt: str,
        output_type: type[T],
        used_families: Mapping[str, str] | None = None,
        prompt_version: str | None = None,
    ) -> CallResult[T]:
        """Ask the best available model for this agent, and keep asking until one answers.

        Failure is expected, not exceptional: free providers rate-limit, rotate models and go
        down. So every attempt is journaled with its outcome — the failures are the dataset that
        tells us which models to stop configuring — and the run only dies when nothing is left.

        A schema failure gets `policy.repair_attempts` repairs (one) on the same model before
        moving on: models that cannot produce the shape twice rarely produce it on the third try,
        and the run has a budget.
        """
        from pydantic import ValidationError
        from pydantic_ai import Agent
        from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
        from pydantic_ai.usage import RunUsage

        # Capped: an unbounded chain of candidates x a 45s timeout, across 15 assets, ate 7m29s
        # of an 8-minute run budget on 2026-09-20. One stubborn asset must not spend the run.
        policy = models_config().get("policy", {})
        limit = int(policy.get("max_attempts", 4))
        repairs = max(0, int(policy.get("repair_attempts", 1)))
        # A request larger than a provider's per-minute token budget is refused (413) every time,
        # so sending it only spends one of the capped attempts (2026-10-08: the batched Trader
        # prompt burned both Groq slots and Mistral was never reached). Skipped before counting.
        need = approx_tokens(prompt)
        planned = self.plan(agent, used_families)
        too_large = [r.label for r in planned if r.tpm and need > r.tpm]
        if too_large:
            log.info("router.skip_too_large", agent=agent, tokens=need, skipped=too_large)
        fits = [r for r in planned if not (r.tpm and need > r.tpm)]
        # The paid last resort sits outside the attempt cap: with a free chain longer than
        # `max_attempts`, a tail inside the cap would never be reached. Its own cap is small.
        candidates = [r for r in fits if not r.paid][:limit] + [r for r in fits if r.paid][
            :PAID_LAST_RESORT_ATTEMPTS
        ]
        if not candidates:
            raise AllModelsFailedError(f"{agent}: no model is currently available")

        errors: list[str] = []
        began = time.perf_counter()
        for attempt, ref in enumerate(candidates, start=1):
            if attempt > 1 and time.perf_counter() - began > self.deadline_s:
                errors.append(f"deadline of {self.deadline_s:.0f}s reached")
                log.warning("router.deadline", agent=agent, attempts=attempt - 1)
                break
            settings = self.model_settings(agent, ref.provider)
            breaker = self.breakers.get(ref.breaker_key)
            if not breaker.allow():
                errors.append(f"{ref.label}: breaker open")
                continue
            # Counted once the breaker lets the call through (a refused call sends nothing and
            # must not burn quota), and before the request so in-flight requests are in the window.
            self.record_attempt(ref)
            started = time.perf_counter()
            repaired = False
            text_prompt = prompt
            # One counter across the first try and its repairs: both are billed (L-7).
            usage = RunUsage()
            try:
                model = self.model_factory(ref)
                for phase in range(repairs + 1):
                    if phase:
                        # A repair is a request of its own; the window must see it too (L-6).
                        self.record_attempt(ref)
                    try:
                        # retries=0: Pydantic AI otherwise re-asks on invalid output inside
                        # run_sync, unseen by headroom — four requests per journaled attempt,
                        # counted as one (L-6). The router's repair is the only retry.
                        answer = Agent(
                            model,
                            output_type=_output_spec(ref, output_type),
                            retries=0,
                            # Named for the role, so a trace reads "trader", not "agent".
                            name=agent,
                        ).run_sync(text_prompt, model_settings=settings, usage=usage)
                        break
                    except (ValidationError, UnexpectedModelBehavior) as exc:
                        if phase == repairs:
                            raise
                        repaired = True
                        text_prompt = (
                            f"{prompt}\n\nYour previous answer was rejected: {exc}\n"
                            "Reply again with valid output only."
                        )
                breaker.record_success()
                call_id = self.record_call(
                    run_id=run_id,
                    agent=agent,
                    tier=agent_policy(agent).tier,
                    ref=ref,
                    attempt=attempt,
                    status="ok",
                    prompt=text_prompt,
                    raw_output=str(answer.output),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_version=prompt_version,
                    # 0 means the provider reported nothing; NULL lets the spend counter charge
                    # the ceiling instead of nothing (L-7).
                    tokens_in=usage.input_tokens or None,
                    tokens_out=usage.output_tokens or None,
                )
                return CallResult(answer.output, ref, attempt, call_id, repaired)
            except Exception as exc:  # noqa: BLE001 - one bad model must not end the run
                rate_limited = isinstance(exc, ModelHTTPError) and exc.status_code == 429
                schema_fail = isinstance(exc, ValidationError | UnexpectedModelBehavior)
                if rate_limited:
                    # "Not now" is not "broken", so the breaker stays shut — but stop
                    # offering this provider until it has had time to recover.
                    status = "rate_limited"
                    self.record_rate_limit(ref, cooldown_seconds(str(exc)))
                elif schema_fail:
                    status = "schema_fail"
                    breaker.record_success()
                else:
                    status = "error"
                    breaker.record_failure()
                detail = f"{type(exc).__name__}: {exc}"
                errors.append(f"{ref.label} {detail}")
                log.warning("router.attempt_failed", agent=agent, model=ref.label, error=detail)
                self.record_call(
                    run_id=run_id,
                    agent=agent,
                    tier=agent_policy(agent).tier,
                    ref=ref,
                    attempt=attempt,
                    status=status,
                    prompt=text_prompt,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_version=prompt_version,
                    error=detail,
                    tokens_in=usage.input_tokens or None,
                    tokens_out=usage.output_tokens or None,
                )
        raise AllModelsFailedError(
            f"{agent}: all {len(candidates)} models failed — " + "; ".join(errors)
        )

    # --- journal ----------------------------------------------------------
    def record_call(
        self,
        run_id: str,
        agent: str,
        tier: str,
        ref: ModelRef,
        attempt: int,
        status: str,
        prompt: str,
        raw_output: str | None = None,
        latency_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        prompt_version: str | None = None,
        error: str | None = None,
    ) -> str:
        """Every attempt, not just the successful one — the failures are the calibration data."""
        call_id = new_id()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_calls (call_id, run_id, created_at, agent, tier, provider,"
                    " model, family, attempt, status, latency_ms, tokens_in, tokens_out,"
                    " prompt_version, prompt, raw_output, error)"
                    " VALUES (:c, :r, :t, :a, :ti, :p, :m, :f, :at, :s, :l, :in, :out, :pv, :pr,"
                    " :ro, :e)"
                ),
                {
                    "c": call_id,
                    "r": run_id,
                    "t": utc_now(),
                    "a": agent,
                    "ti": tier,
                    "p": ref.provider,
                    "m": ref.model,
                    "f": ref.family,
                    "at": attempt,
                    "s": status,
                    "l": latency_ms,
                    "in": tokens_in,
                    "out": tokens_out,
                    "pv": prompt_version,
                    "pr": prompt,
                    "ro": raw_output,
                    "e": error,
                },
            )
        return call_id
