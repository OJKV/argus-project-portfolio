"""Suite-wide isolation: tests never read the developer's real .env or write into ./var.

antigravity review H2 pointed at `_env_file=None`; the real defect next to it was that any code
path calling `get_settings()` read owner's actual `.env` — so adding a CMC key would have made a
unit test hit the real CMC API, and the disk cache landed in the real ./var/cache.
Tests marked `demo` opt out: they exist to exercise the real credentials.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from argus import config

SECRET_ENV_PREFIXES = (
    "OKX_",
    "BYBIT_",
    "OPENROUTER_",
    "GEMINI_",
    "GROQ_",
    "MISTRAL_",
    "NVIDIA_",
    "CLOUDFLARE_",
    "GITHUB_TOKEN",
    "ZHIPU_",
    "SAMBANOVA_",
    "DEEPSEEK_",
    "COINALYZE_",
    "COINGECKO_",
    "CMC_",
    "CRYPTOPANIC_",
    "FRED_",
    "ETHERSCAN_",
    "DUNE_",
    "SCIRA_",
    "TELEGRAM_",
    "LOGFIRE_",
    "HEALTHCHECKS_",
    "MODE",
    "LIVE_TRADING",
    "LIVE_VENUE",
    "RISK_PROFILE",
    "ARGUS_HOME",
)


@pytest.fixture(autouse=True)
def _isolated_settings(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("demo"):
        # Real-venue tests need the real keys from .env, so the secrets are left alone. The
        # journal is not: without this they inherited ARGUS_HOME and wrote their runs, plans and
        # orders into the production journal on the VPS (F-154).
        monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "argus_home"))
        config.get_settings.cache_clear()
        yield
        config.get_settings.cache_clear()
        return
    import os

    for name in list(os.environ):
        if name.upper().startswith(SECRET_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "argus_home"))
    config.get_settings.cache_clear()
    from argus.data import cache

    monkeypatch.setattr(cache, "_cache", None)
    yield
    config.get_settings.cache_clear()
    if cache._cache is not None:
        cache._cache.close()


@pytest.fixture
def live_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the demo account on the live decision profile — the revert switch in
    `argus/gate/limits.py`. For tests that encode live behaviour through `run_manager`, whose
    isolated settings are the demo account (DECISION_LOGIC §11)."""
    from argus.gate import limits

    monkeypatch.setattr(limits, "DEMO_PROFILE", limits.LIVE_CONSERVATIVE)
