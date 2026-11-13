"""OKX credential selection and host interlocks (FAILURE_LOG F-034, F-035)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from argus.config import Settings


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "mode": "demo",
        "okx_demo": True,
        "okx_api_key": "live-k",
        "okx_api_secret": "live-s",
        "okx_api_passphrase": "live-p",
        "okx_demo_api_key": "demo-k",
        "okx_demo_api_secret": "demo-s",
        "okx_demo_api_passphrase": "demo-p",
    }
    base.update(over)
    return Settings(**base)


def test_demo_mode_uses_demo_credentials_only() -> None:
    s = _settings()
    assert s.okx_credentials == ("demo-k", "demo-s", "demo-p")
    assert s.okx_credentials_set


def test_live_mode_uses_live_credentials() -> None:
    s = _settings(
        okx_demo=False,
        mode="auto",
        live_trading="I_UNDERSTAND",
        live_venue="I_UNDERSTAND_REAL_MONEY",  # P-N5: real money takes the second word
    )
    assert s.okx_credentials == ("live-k", "live-s", "live-p")


def test_missing_demo_triple_is_reported_not_silently_falling_back_to_live() -> None:
    s = _settings(okx_demo_api_key="")
    assert not s.okx_credentials_set
    assert s.okx_credentials[0] == ""


def test_public_only_okx_edges_are_rejected_as_the_authenticated_host() -> None:
    # my./app./eea.okx.com answer 401 code=50119 for every key — never the auth host.
    for host in ("https://app.okx.com", "https://my.okx.com", "https://eea.okx.com"):
        with pytest.raises(ValidationError, match="50119"):
            _settings(okx_host=host)


def test_public_host_override_does_not_touch_the_authenticated_host() -> None:
    s = _settings(okx_public_host_override="https://app.okx.com")
    assert s.okx_host == "https://www.okx.com"
    assert s.okx_public_host == "https://app.okx.com"


def test_live_mode_refuses_without_the_interlock() -> None:
    with pytest.raises(ValidationError, match="I_UNDERSTAND"):
        _settings(mode="auto", okx_demo=False)


def test_arming_against_the_demo_venue_is_allowed() -> None:
    """The rehearsal quadrant, allowed since 2026-09-24 (F-123).

    This used to be refused on the reasoning that `auto|approve` means live. The effect was that
    the execution path could only ever be exercised with real money — the opposite of what a demo
    venue exists for. Arming against demo runs the identical code against fake funds.

    `is_live` means armed; `venue` says which account. Conflating them was the original mistake.
    """
    s = _settings(mode="auto", live_trading="I_UNDERSTAND", okx_demo=True)
    assert s.is_live is True
    assert s.venue == "demo"


def test_arming_still_needs_the_interlock_on_either_venue() -> None:
    for demo in (True, False):
        with pytest.raises(ValidationError, match="LIVE_TRADING"):
            _settings(mode="approve", okx_demo=demo)


def test_demo_mode_cannot_point_at_the_live_exchange() -> None:
    """Review 2026-09-18 S-1: the fourth quadrant reached the live account with no interlock.

    `_live_interlock` only checked auth in one direction (auto/approve must be live). The
    combination MODE=demo + OKX_DEMO=false validated cleanly, loaded the **live** key triple with
    no sandbox header, and let `argus watchdog --once` amend stops and close real positions — while
    the journal stamped every row `mode=demo`. Both key triples live in one `.env`; one wrong line
    was enough.
    """
    with pytest.raises(ValidationError) as exc:
        Settings(mode="demo", okx_demo=False, _env_file=None)
    assert "OKX_DEMO" in str(exc.value)


def test_demo_mode_with_the_demo_exchange_is_fine() -> None:
    assert Settings(mode="demo", okx_demo=True, _env_file=None).okx_demo is True


def test_a_lookalike_host_is_refused() -> None:
    """Review 2026-09-18 S-4: `startswith` accepted any domain that merely began with the real one.

    `https://www.okx.com.evil.tld` passed validation and would have received signed API requests.
    """
    with pytest.raises(ValidationError):
        Settings(
            okx_host="https://www.okx.com.evil.tld",
            mode="demo",
            okx_demo=True,
            _env_file=None,
        )


def test_the_real_host_is_accepted() -> None:
    host = "https://www.okx.com"
    assert Settings(okx_host=host, _env_file=None).okx_host == host


def test_credentials_can_be_requested_for_an_environment_explicitly() -> None:
    """A read-only check of the live keys must not require arming live trading.

    `okx_credentials` follows `OKX_DEMO`, so the only way to look at the live account used to be
    to flip MODE — which then demands LIVE_TRADING=I_UNDERSTAND and arms order placement. Asking
    for a triple by name keeps "are the live keys valid and is the IP binding right?" a question
    you can answer without loading a weapon to check it is clean.
    """
    settings = Settings(
        mode="demo",
        okx_demo=True,
        okx_api_key="live-k",
        okx_api_secret="live-s",
        okx_api_passphrase="live-p",
        okx_demo_api_key="demo-k",
        okx_demo_api_secret="demo-s",
        okx_demo_api_passphrase="demo-p",
        _env_file=None,
    )
    assert settings.okx_credentials == ("demo-k", "demo-s", "demo-p")  # unchanged default
    assert settings.okx_credentials_for("live") == ("live-k", "live-s", "live-p")
    assert settings.okx_credentials_for("demo") == ("demo-k", "demo-s", "demo-p")


def test_asking_for_an_unknown_environment_is_an_error() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None).okx_credentials_for("mainnet")  # type: ignore[arg-type]
