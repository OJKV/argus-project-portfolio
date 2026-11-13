"""The live venue has its own interlock (review 2026-10-07, P-N5).

Arming the demo account needs `LIVE_TRADING=I_UNDERSTAND` (F-123), so that word is already set on
the VPS. From there, real money was one more variable — `OKX_DEMO=false` — with nothing else to
type. AGENTS.md rule 1 no longer told demo from live. `LIVE_VENUE=I_UNDERSTAND_REAL_MONEY` is the
second, separate word, and it means only "this may reach the live account".
"""

from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from argus.config import Settings

REAL_MONEY = "I_UNDERSTAND_REAL_MONEY"


@pytest.fixture(autouse=True)
def _no_venue_word_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_VENUE", raising=False)


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.mark.parametrize("mode", ["approve", "auto"])
def test_arming_the_live_account_needs_the_second_word(mode: str) -> None:
    with pytest.raises(ValidationError, match="LIVE_VENUE"):
        _settings(mode=mode, live_trading="I_UNDERSTAND", okx_demo=False)


@pytest.mark.parametrize("mode", ["approve", "auto"])
def test_with_both_words_the_live_account_is_armed(mode: str) -> None:
    s = _settings(mode=mode, live_trading="I_UNDERSTAND", okx_demo=False, live_venue=REAL_MONEY)
    assert s.is_live and s.venue == "live"


@pytest.mark.parametrize("mode", ["approve", "auto"])
def test_the_demo_rehearsal_needs_only_the_first_word(mode: str) -> None:
    """F-123's quadrant stays open: arming against fake funds must not require the money word."""
    s = _settings(mode=mode, live_trading="I_UNDERSTAND", okx_demo=True)
    assert s.is_live and s.venue == "demo"


@pytest.mark.parametrize("word", ["I_UNDERSTAND", "i_understand_real_money", "yes", " "])
def test_only_the_exact_word_counts(word: str) -> None:
    with pytest.raises(ValidationError, match="LIVE_VENUE"):
        _settings(mode="auto", live_trading="I_UNDERSTAND", okx_demo=False, live_venue=word)


def test_the_venue_word_alone_arms_nothing() -> None:
    """It is an extra key for the live account, not a substitute for LIVE_TRADING."""
    with pytest.raises(ValidationError, match="LIVE_TRADING"):
        _settings(mode="auto", okx_demo=False, live_venue=REAL_MONEY)


def test_every_quadrant() -> None:
    """mode x venue x the two words: exactly the combinations listed in config.py are allowed."""
    allowed = set()
    for mode, demo, trading, venue in itertools.product(
        ("demo", "approve", "auto"), (True, False), ("", "I_UNDERSTAND"), ("", REAL_MONEY)
    ):
        try:
            _settings(mode=mode, okx_demo=demo, live_trading=trading, live_venue=venue)
        except ValidationError:
            continue
        allowed.add((mode, demo, trading, venue))

    expected = {("demo", True, t, v) for t in ("", "I_UNDERSTAND") for v in ("", REAL_MONEY)}
    for mode in ("approve", "auto"):
        expected |= {(mode, True, "I_UNDERSTAND", v) for v in ("", REAL_MONEY)}
        expected.add((mode, False, "I_UNDERSTAND", REAL_MONEY))
    assert allowed == expected


def test_doctor_shows_whether_the_live_venue_word_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from argus import config, doctor

    monkeypatch.setenv("LIVE_VENUE", REAL_MONEY)
    config.get_settings.cache_clear()
    (row,) = [c for c in doctor.check_config() if c.name == "live-venue interlock"]
    assert row.detail.startswith("SET")

    monkeypatch.delenv("LIVE_VENUE")
    config.get_settings.cache_clear()
    (row,) = [c for c in doctor.check_config() if c.name == "live-venue interlock"]
    assert "not set" in row.detail
