"""The suite must never see the developer's real environment (see tests/conftest.py)."""

from __future__ import annotations

from pathlib import Path

from argus.config import get_settings
from argus.data.cache import cache_dir


def test_settings_do_not_come_from_the_real_env_file() -> None:
    settings = get_settings()
    assert not settings.okx_credentials_set
    assert not settings.has("cmc_api_key")
    assert settings.mode == "demo"


def test_state_is_written_under_a_temporary_home(tmp_path: Path) -> None:
    home = get_settings().argus_home
    assert str(home).startswith(str(tmp_path.parent.parent))
    assert cache_dir().is_relative_to(home)
