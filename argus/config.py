"""Settings: env (.env) + YAML config files. Never log or print secrets."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from argus.gate.limits import DecisionLimits, limits_for

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"

Mode = Literal["demo", "auto", "approve"]
# The exact value LIVE_VENUE must hold before any armed run may reach the live OKX account.
LIVE_VENUE_WORD = "I_UNDERSTAND_REAL_MONEY"
# Said wherever an armed run announces itself (review 2026-10-07, P-N2). `approve` has no approval
# step: nothing writes a QUEUED plan, there is no /approve, and the executor places on the gate's
# approval alone. The name promises a wait that does not happen, so the system says so instead.
APPROVE_IS_AUTO = "MODE=approve has no approval step yet: it places orders exactly like MODE=auto"


class Settings(BaseSettings):
    # extra="ignore" stays: a real .env may hold variables for other tools. A typo of one of OUR
    # keys is caught by `unknown_env_keys` (doctor warns) instead, and hide_input_in_errors keeps
    # a validation error from echoing a secret's value (review S-5).
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    mode: Mode = "demo"
    live_trading: str = ""
    # The second word, for real money only. LIVE_TRADING arms the demo rehearsal too (F-123), so on
    # its own it no longer says which account a mistake would cost (P-N5).
    live_venue: str = ""
    risk_profile: Literal["", "conservative", "aggressive"] = ""
    argus_home: Path = Path("/var/lib/argus")
    tz_display: str = "Asia/Almaty"

    okx_api_key: SecretStr = SecretStr("")
    okx_api_secret: SecretStr = SecretStr("")
    okx_api_passphrase: SecretStr = SecretStr("")
    okx_demo_api_key: SecretStr = SecretStr("")
    okx_demo_api_secret: SecretStr = SecretStr("")
    okx_demo_api_passphrase: SecretStr = SecretStr("")
    okx_demo: bool = True
    # A dual-stack host prefers IPv6 by default, so OKX would see an address the key was not
    # bound to and reject every authenticated call with 50110 (F-060). OKX's allowlist does
    # accept IPv6 — both keys now carry the VPS's v6 address as well — but one predictable
    # egress family is still worth having: it is the address the keys were cut against, it is
    # what `doctor --live-keys` verifies, and a silent switch to v6 is a failure nobody would
    # see until an order was rejected.
    force_ipv4: bool = True
    # my./app./eea.okx.com serve public data but reject every key with 401 code=50119,
    # so the authenticated host must stay www.okx.com (VPN required from KZ).
    okx_host: str = "https://www.okx.com"
    # Public-data-only fallback edge (my./app./eea.okx.com) for when the VPN is off.
    okx_public_host_override: str = ""
    bybit_kz_api_key: SecretStr = SecretStr("")
    bybit_kz_api_secret: SecretStr = SecretStr("")
    bybit_kz_host: str = "https://api.bybit.kz"

    openrouter_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    mistral_api_key: SecretStr = SecretStr("")
    nvidia_api_key: SecretStr = SecretStr("")
    cloudflare_api_token: SecretStr = SecretStr("")
    cloudflare_account_id: str = ""
    zhipu_api_key: SecretStr = SecretStr("")
    sambanova_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    # Paid fallbacks, in order: DeepSeek first (cents per run), OpenAI last (dollars).
    openai_api_key: SecretStr = SecretStr("")
    # Per-provider monthly caps on paid LLM spend (owner, 2026-10-26): $5 each. A paid provider with
    # no entry has a cap of zero, so adding one to models.yaml cannot spend until it is capped here.
    paid_monthly_cap_usd_by_provider: dict[str, float] = {"deepseek": 5.0, "openai": 5.0}
    # Overall ceiling over all providers together (the sum of the two caps above). Also what an
    # unpriced history row trips: its provider's spend becomes unknowable, so the sum is infinite.
    paid_llm_monthly_cap_usd: float = 10.0

    coinalyze_api_key: SecretStr = SecretStr("")
    coingecko_demo_api_key: SecretStr = SecretStr("")
    cmc_api_key: SecretStr = SecretStr("")
    cryptopanic_api_key: SecretStr = SecretStr("")
    cryptopanic_api_key_2: SecretStr = SecretStr("")
    fred_api_key: SecretStr = SecretStr("")
    etherscan_api_key: SecretStr = SecretStr("")
    dune_api_key: SecretStr = SecretStr("")
    scira_api_key: SecretStr = SecretStr("")
    edgar_user_agent: str = ""

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    logfire_token: SecretStr = SecretStr("")
    healthchecks_url: str = ""
    # Alert thresholds (OBSERVABILITY_AND_GUARDRAILS.md §1): a manager run that outgrows its
    # budget will eventually collide with the next schedule; silence is the failure nobody sees.
    run_budget_seconds: int = 480
    manager_max_gap_hours: float = 14.0

    @model_validator(mode="after")
    def _absolute_home(self) -> Settings:
        # The journal anchored a relative ARGUS_HOME at the repo root, the run lock and the HALT
        # file at the current directory — so a run started from another directory used a
        # different lock and a different HALT file over the same journal (review J-9). Anchor it
        # once, here, the way the journal already did.
        if not self.argus_home.is_absolute():
            self.argus_home = (REPO_ROOT / self.argus_home).resolve()
        return self

    @model_validator(mode="after")
    def _live_interlock(self) -> Settings:
        # Both directions. Checking only auth left a fourth quadrant — MODE=demo with
        # OKX_DEMO=false — that validated cleanly, loaded the live key triple with no sandbox
        # header, and let the watchdog amend stops and close real positions while every journal
        # row said `mode=demo` (review 2026-09-18, S-1).
        if self.mode == "demo" and not self.okx_demo:
            raise ValueError(
                "MODE=demo requires OKX_DEMO=true — MODE=demo with OKX_DEMO=false points the "
                "adapter at the live account with no LIVE_TRADING interlock"
            )
        if self.mode in ("auto", "approve") and self.live_trading != "I_UNDERSTAND":
            raise ValueError("MODE=auto|approve requires LIVE_TRADING=I_UNDERSTAND")
        # A separate word for the live account (review 2026-10-07, P-N5). LIVE_TRADING has to be
        # set to arm the demo rehearsal, so once the VPS was armed on demo, real money was a
        # single edit away — OKX_DEMO=false — with nothing else to type and nothing that told the
        # two apart. This word is meaningless on the demo venue, so it can never be "left set
        # from the rehearsal"; setting it is a decision about real money and nothing else.
        if self.mode in ("auto", "approve") and not self.okx_demo:
            if self.live_venue != LIVE_VENUE_WORD:
                raise ValueError(
                    f"MODE={self.mode} with OKX_DEMO=false arms the LIVE account and requires "
                    f"LIVE_VENUE={LIVE_VENUE_WORD} as well as LIVE_TRADING=I_UNDERSTAND"
                )
        # `MODE=approve` with `OKX_DEMO=true` is ALLOWED, and deliberately so. It used to be
        # refused on the reasoning that approve means live — but the effect was that the
        # execution path could only ever be exercised with real money, which is the opposite of
        # what a demo venue is for. Arming against the demo account runs the identical code
        # against fake funds, which is the only safe way to rehearse it (F-123).
        #
        # The four quadrants now read:
        #   demo    + OKX_DEMO=true   research and shadow only
        #   demo    + OKX_DEMO=false  REFUSED — live account with no interlock (S-1)
        #   approve + OKX_DEMO=true   armed against the DEMO venue — the rehearsal
        #   approve + OKX_DEMO=false  armed against the LIVE account — real money, and only with
        #                             LIVE_VENUE=I_UNDERSTAND_REAL_MONEY as well (P-N5)
        #
        # Arming takes LIVE_TRADING=I_UNDERSTAND either way; reaching real money takes the second
        # word on top. `auto` reads the same as `approve` in every row (P-N2: approve has no
        # approval step yet).
        # Exact match: `startswith` accepted https://www.okx.com.evil.tld, which would then
        # have received signed API requests (review 2026-09-18, S-4).
        if self.okx_host != "https://www.okx.com":
            raise ValueError(
                "OKX_HOST must be https://www.okx.com — other OKX edges serve public data "
                "only and reject API keys with 401 code=50119"
            )
        return self

    @property
    def is_live(self) -> bool:
        """Armed to place orders. Says nothing about WHICH account — see `venue`."""
        return self.mode in ("auto", "approve")

    @property
    def venue(self) -> str:
        """`demo` or `live`. The distinction `is_live` does not make, and the one that decides
        whether a mistake costs money."""
        return "demo" if self.okx_demo else "live"

    @property
    def limits(self) -> DecisionLimits:
        """The decision profile for this account — the single place it is chosen (DECISION_LOGIC
        §11). Decided by the venue alone; no agent output reaches it (non-negotiable #3)."""
        return limits_for(self.venue)

    @property
    def okx_public_host(self) -> str:
        """Host for unauthenticated market data; may be an edge that rejects API keys."""
        return self.okx_public_host_override or self.okx_host

    def okx_credentials_for(self, env: str) -> tuple[str, str, str]:
        """(key, secret, passphrase) for a named environment, ignoring OKX_DEMO.

        Exists so a **read-only** check of the live keys does not require flipping MODE, which
        would demand LIVE_TRADING=I_UNDERSTAND and arm order placement. Verifying that the live
        keys work and that the IP binding is right should not mean loading the weapon to check
        that it is clean.
        """
        if env == "demo":
            return (
                self.okx_demo_api_key.get_secret_value(),
                self.okx_demo_api_secret.get_secret_value(),
                self.okx_demo_api_passphrase.get_secret_value(),
            )
        if env == "live":
            return (
                self.okx_api_key.get_secret_value(),
                self.okx_api_secret.get_secret_value(),
                self.okx_api_passphrase.get_secret_value(),
            )
        raise ValueError(f"unknown OKX environment {env!r} — expected 'demo' or 'live'")

    @property
    def okx_credentials(self) -> tuple[str, str, str]:
        """(key, secret, passphrase) for the environment in use. Demo keys never trade live."""
        return self.okx_credentials_for("demo" if self.okx_demo else "live")

    @property
    def okx_credentials_set(self) -> bool:
        return all(self.okx_credentials)

    @property
    def halt_file(self) -> Path:
        return self.argus_home / "HALT"

    @property
    def journal_path(self) -> Path:
        return self.argus_home / "journal.db"

    def has(self, name: str) -> bool:
        """True if a secret/setting is non-empty (for doctor; never returns the value)."""
        value = getattr(self, name)
        if isinstance(value, SecretStr):
            return bool(value.get_secret_value())
        return bool(value)


def load_yaml(name: str, base: Path = CONFIG_DIR) -> dict[str, Any]:
    path = base / name
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def unknown_env_keys(env_file: Path | str = ".env") -> dict[str, str]:
    """Keys in the env file that are a near-miss of a Settings field: {typo: intended}.

    Names only, never values. Keys unlike any field are other tools' variables and pass.
    """
    import difflib

    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    fields = [n.upper() for n in Settings.model_fields]
    out: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip().upper()
        if key and key not in fields:
            near = difflib.get_close_matches(key, fields, n=1, cutoff=0.85)
            if near:
                out[key] = near[0]
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def risk_config() -> dict[str, Any]:
    return load_yaml("risk_profiles.yaml")


@lru_cache(maxsize=1)
def models_config() -> dict[str, Any]:
    return load_yaml("models.yaml")


@lru_cache(maxsize=1)
def universe_config() -> dict[str, Any]:
    return load_yaml("universe.yaml", KNOWLEDGE_DIR)


__all__ = [
    "APPROVE_IS_AUTO",
    "CONFIG_DIR",
    "KNOWLEDGE_DIR",
    "LIVE_VENUE_WORD",
    "REPO_ROOT",
    "Field",
    "Settings",
    "get_settings",
    "unknown_env_keys",
    "load_yaml",
    "models_config",
    "risk_config",
    "universe_config",
]
