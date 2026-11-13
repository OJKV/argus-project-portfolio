import pytest
from pydantic import ValidationError

from argus.config import Settings


def test_demo_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODE", raising=False)
    s = Settings()
    assert s.mode == "demo" and not s.is_live


def test_live_requires_interlock() -> None:
    # Arming needs the interlock whichever account it points at.
    with pytest.raises(ValidationError):
        Settings(mode="auto", okx_demo=False)
    with pytest.raises(ValidationError):
        Settings(mode="auto", okx_demo=True)

    live = Settings(
        mode="auto",
        live_trading="I_UNDERSTAND",
        okx_demo=False,
        live_venue="I_UNDERSTAND_REAL_MONEY",  # the live account's own word (P-N5)
    )
    assert live.is_live and live.venue == "live"
    # Armed against the demo venue is the rehearsal, and it is allowed (F-123).
    rehearsal = Settings(mode="auto", live_trading="I_UNDERSTAND", okx_demo=True)
    assert rehearsal.is_live and rehearsal.venue == "demo"


def test_secrets_never_repr() -> None:
    s = Settings(okx_api_key="abc")
    assert "abc" not in repr(s) and s.has("okx_api_key")


def test_a_telegram_failure_never_logs_the_bot_token() -> None:
    """httpx puts the request URL in its error message, and the bot token IS part of that URL.

    The obvious `f"{exc}"` wrote the token into journald on every failed send, and would have
    sent it to Logfire too — the same shape as F-070, one channel over (F-133).
    """
    from argus.notify.telegram import scrub_token

    token = "8876849535:AAEaJUk5EhNtIdP8vgiybpFLw_KBXaRqGqU"
    exc = RuntimeError(
        f"Client error '403 Forbidden' for url 'https://api.telegram.org/bot{token}/sendMessage'"
    )
    scrubbed = scrub_token(exc)

    assert token not in scrubbed
    assert token.split(":")[0] not in scrubbed
    assert "<redacted>" in scrubbed
    # The useful part survives: without the status nobody can tell a 403 from a timeout.
    assert "403" in scrubbed
