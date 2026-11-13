"""The P1 alert reaches the phone whatever the exception text contains (review O-1 / J-N9).

ccxt and urllib3 errors read like `<urllib3.connection.HTTPSConnection object at 0x…>`. Sent with
`parse_mode=HTML` and interpolated raw, Telegram answered 400 "can't parse entities", the notifier
swallowed it, and the message most likely to carry such text — "stop repair FAILED, close
manually" — was the one that never arrived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
import pytest

from argus.notify.telegram import Notifier

MAX_MESSAGE = 4096

OWNER = "424242"
URLLIB3 = "ConnectError: <urllib3.connection.HTTPSConnection object at 0x10f> & retries=3"

_ALLOWED_TAG = re.compile(r"</?(b|i|u|s|code|pre)>")
_ENTITY = re.compile(r"&(lt|gt|amp|quot|#\d+);")


def _parse_error(message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.telegram.org/botX/sendMessage")
    response = httpx.Response(400, request=request, json={"description": message})
    return httpx.HTTPStatusError(message, request=request, response=response)


@dataclass
class TelegramDouble:
    """Refuses what Telegram refuses: bad HTML entities, and anything over 4096 characters."""

    reject_all_html: bool = False
    delivered: list[tuple[str, str | None]] = field(default_factory=list)
    attempts: int = 0

    def send(self, chat_id: str, text: str, parse_mode: str | None = None) -> None:
        self.attempts += 1
        if len(text) > 4096:
            raise _parse_error("Bad Request: message is too long")
        if parse_mode == "HTML":
            if self.reject_all_html:
                raise _parse_error("Bad Request: can't parse entities")
            stripped = _ENTITY.sub("", _ALLOWED_TAG.sub("", text))
            if "<" in stripped or ">" in stripped or "&" in stripped:
                raise _parse_error("Bad Request: can't parse entities: unsupported start tag")
        self.delivered.append((text, parse_mode))


def test_repair_failed_with_angle_brackets_in_the_error_is_delivered() -> None:
    bot = TelegramDouble()
    assert Notifier(bot=bot, chat_id=OWNER).repair_failed("BTC-USDT-SWAP", URLLIB3) is True
    text, mode = bot.delivered[-1]
    assert mode == "HTML"
    assert "&lt;urllib3.connection.HTTPSConnection" in text  # escaped, not dropped
    assert "<b>" in text  # the formatting survives


def test_every_interpolated_field_is_escaped() -> None:
    bot = TelegramDouble()
    notifier = Notifier(bot=bot, chat_id=OWNER)
    assert notifier.unprotected_repaired("<x>", "1 & 2", "<plan>") is True
    assert notifier.drawdown_halt("<1>", "a&b", "<c>") is True
    assert notifier.margin_warning("<sym>", "<r>") is True
    assert notifier.exchange_unreachable(3) is True
    assert len(bot.delivered) == 4


def test_a_huge_error_is_capped_and_still_delivered() -> None:
    bot = TelegramDouble()
    assert Notifier(bot=bot, chat_id=OWNER).repair_failed("ETH", "x" * 20_000) is True
    text, _ = bot.delivered[-1]
    assert len(text) <= MAX_MESSAGE
    assert "Close the position manually" in text  # the instruction is never the part cut off


def test_plain_text_is_sent_without_parse_mode() -> None:
    """Alert rules and raw notes carry `<` too; they go out as plain text."""
    bot = TelegramDouble()
    assert Notifier(bot=bot, chat_id=OWNER).send(f"[CRITICAL] x: {URLLIB3}") is True
    assert bot.delivered[-1] == (f"[CRITICAL] x: {URLLIB3}", None)


def test_a_rejected_html_message_is_retried_once_as_plain_text() -> None:
    bot = TelegramDouble(reject_all_html=True)
    assert Notifier(bot=bot, chat_id=OWNER).repair_failed("SOL", URLLIB3) is True
    assert bot.attempts == 2
    text, mode = bot.delivered[-1]
    assert mode is None
    assert URLLIB3 in text  # the raw error, readable
    assert "<b>" not in text and "&lt;" not in text


def test_a_long_plain_message_is_capped() -> None:
    bot = TelegramDouble()
    assert Notifier(bot=bot, chat_id=OWNER).send("y" * 10_000) is True
    assert len(bot.delivered[-1][0]) <= MAX_MESSAGE


def test_the_real_http_bot_sends_escaped_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through `HttpBot` itself, the way the watchdog sends: the payload Telegram receives."""
    from argus.notify import telegram

    double = TelegramDouble()

    def fake_post(url: str, json: dict[str, str], timeout: float) -> httpx.Response:
        double.send(json["chat_id"], json["text"], json.get("parse_mode"))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(telegram.httpx, "post", fake_post)
    notifier = Notifier(bot=telegram.HttpBot("123:abc"), chat_id=OWNER)
    assert notifier.repair_failed("BTC-USDT-SWAP", URLLIB3) is True
    assert double.delivered[-1][1] == "HTML"
