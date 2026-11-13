"""Telegram alerts + control (ARCHITECTURE.md §9).

Two rules:
  * every command is refused unless it comes from the owner, in TELEGRAM_CHAT_ID;
  * a missing token is not an error — alerts degrade to structured logs so a watchdog run never
    fails because notification is down. Silence must be visible, not fatal.

The command side (`CommandRouter`, `build_router`) is NOT WIRED as of 2026-10-07: nothing polls
Telegram for updates and nothing in `argus/` constructs a router, so /halt, /resume and /close
do nothing yet (review O-2 / P-2). The working controls are the CLI commands `argus halt "<why>"`,
`argus resume [--after-kill-switch]` and `argus flatten --yes [--symbol X]`,
or the HALT file / `systemctl stop`. When a
poller is added it must pass the sender's `from.id` to `dispatch`: in a group, `chat.id` is the
group, and checking only that let every member /resume or /close.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

from argus.config import Settings, get_settings

log = structlog.get_logger(__name__)

# `https://api.telegram.org/bot<TOKEN>/sendMessage` — the token sits between "bot" and the next
# slash, which is why any message carrying this URL is a secret.
_TOKEN_IN_URL = re.compile(r"(api\.telegram\.org/bot)[^/\s'\"]+", re.I)


def scrub_token(exc: BaseException | str) -> str:
    """An exception's text with any bot token removed. Used on every path that logs a failure."""
    return _TOKEN_IN_URL.sub(
        r"\1<redacted>",
        f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc),
    )


API = "https://api.telegram.org"

# Telegram's limit for one message. Longer is a 400, which used to mean a silently dropped alert.
MAX_MESSAGE = 4096
# Each interpolated value is cut to this before escaping. Escaping can grow text up to five-fold
# (`&` -> `&amp;`), so this keeps the longest message well under MAX_MESSAGE — and the cut falls
# inside the exception text, never inside the instruction that follows it.
MAX_FIELD = 600
# The only tags our templates use. Escaped values cannot contain them, which is what lets the
# plain-text fallback strip exactly these and get the original words back.
_FORMAT_TAGS = re.compile(r"</?(?:b|i|code|pre)>")


def _cap(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


class Bot(Protocol):
    """Minimal surface we need — lets tests pass a recorder instead of a network client."""

    def send(self, chat_id: str, text: str, parse_mode: str | None = None) -> None: ...


@dataclass
class HttpBot:
    token: str
    timeout: float = 10.0

    def send(self, chat_id: str, text: str, parse_mode: str | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        httpx.post(
            f"{API}/bot{self.token}/sendMessage", json=payload, timeout=self.timeout
        ).raise_for_status()


@dataclass
class RecordingBot:
    """Test double; also what a token-less deployment uses so alerts are still inspectable."""

    sent: list[tuple[str, str]] = field(default_factory=list)
    modes: list[str | None] = field(default_factory=list)

    def send(self, chat_id: str, text: str, parse_mode: str | None = None) -> None:
        self.sent.append((chat_id, text))
        self.modes.append(parse_mode)


@dataclass
class Notifier:
    bot: Bot | None
    chat_id: str

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Notifier:
        s = settings or get_settings()
        token = s.telegram_bot_token.get_secret_value()
        if not token or not s.telegram_chat_id:
            log.warning("telegram.disabled", reason="token or chat id missing")
            return cls(bot=None, chat_id=s.telegram_chat_id)
        return cls(bot=HttpBot(token), chat_id=s.telegram_chat_id)

    def send(self, text: str) -> bool:
        """Plain text, best-effort. Returns whether it reached Telegram; never raises.

        No `parse_mode`: alert rules and notes carry exception text, and exception text carries
        `<…>`. Plain text cannot fail to parse.
        """
        return self._deliver(_cap(text), None)

    def send_html(self, template: str, **values: object) -> bool:
        """`template` is trusted HTML; every value is escaped into it (review O-1).

        Interpolating raw `<urllib3.connection.HTTPSConnection object at 0x…>` under
        `parse_mode=HTML` made Telegram answer 400 "can't parse entities", and the send was
        swallowed — so "stop repair FAILED, close manually", the message most likely to carry
        such text, was the one least likely to arrive. If Telegram still rejects the HTML, the
        same words go once more as plain text: an ugly alert beats a missing one.
        """
        rendered = template.format(
            **{k: html.escape(str(v)[:MAX_FIELD], quote=False) for k, v in values.items()}
        )
        plain = html.unescape(_FORMAT_TAGS.sub("", rendered))
        if len(rendered) > MAX_MESSAGE:
            return self._deliver(_cap(plain), None)
        if self.bot is None or not self.chat_id:
            log.info("telegram.would_send", text=plain)
            return False
        try:
            self.bot.send(self.chat_id, rendered, parse_mode="HTML")
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                log.error("telegram.send_failed", error=scrub_token(exc))
                return False
            log.warning("telegram.html_rejected", error=scrub_token(exc), retry="plain text")
            return self._deliver(_cap(plain), None)
        except Exception as exc:  # noqa: BLE001 - notification must never break a run
            log.error("telegram.send_failed", error=scrub_token(exc))
            return False

    def _deliver(self, text: str, parse_mode: str | None) -> bool:
        if self.bot is None or not self.chat_id:
            log.info("telegram.would_send", text=text)
            return False
        try:
            if parse_mode is None:
                self.bot.send(self.chat_id, text)
            else:
                self.bot.send(self.chat_id, text, parse_mode=parse_mode)
            return True
        except Exception as exc:  # noqa: BLE001 - notification must never break a run
            # NEVER interpolate the exception raw. httpx puts the request URL in its message and
            # the bot token IS a path segment of that URL, so the obvious one-liner wrote the
            # token into journald on every failed send — and would have sent it to Logfire too
            # (F-133, the same shape as F-070 one channel over).
            log.error("telegram.send_failed", error=scrub_token(exc))
            return False

    # --- alert vocabulary (ARCHITECTURE.md §9 event feed) ---------------
    def unprotected_repaired(self, symbol: str, stop: str, source: str) -> bool:
        return self.send_html(
            "🛠 <b>Stop repaired</b> {symbol}\nstop={stop} (from {source})\n"
            "A position existed without exchange-side protection.",
            symbol=symbol,
            stop=stop,
            source=source,
        )

    def repair_failed(self, symbol: str, error: str) -> bool:
        return self.send_html(
            "🚨 <b>P1: stop repair FAILED</b> {symbol}\n{error}\n"
            "Close the position manually on OKX, then investigate.",
            symbol=symbol,
            error=error,
        )

    def exchange_unreachable(self, checks: int) -> bool:
        return self.send(
            f"⚠️ Exchange unreachable for {checks} consecutive watchdog checks.\n"
            "Exchange-side stops still protect open positions."
        )

    def drawdown_halt(self, dd_pct: str, equity: str, peak: str) -> bool:
        return self.send_html(
            "🛑 <b>HALT: drawdown kill-switch</b>\ndrawdown {dd_pct}% "
            "(equity {equity} vs 30d high {peak})\nAll positions closed. "
            "Resume only after a FAILURE_LOG entry.",
            dd_pct=dd_pct,
            equity=equity,
            peak=peak,
        )

    def margin_warning(self, symbol: str, ratio: str) -> bool:
        return self.send(f"⚠️ Margin ratio {ratio}% on {symbol} — below the 200% floor.")


# --- command handling ---------------------------------------------------

CommandHandler = Callable[[list[str]], str]


@dataclass
class CommandRouter:
    """Dispatches /commands, refusing anything that is not from the owner, in the owner's chat.

    `owner_id` is the Telegram user allowed to command the bot. Left unset it is `chat_id`, which
    is exactly the owner in a private chat (a private chat's id is the user's id) and matches no
    member of a group — so the default refuses a group rather than trusting all of it.
    """

    chat_id: str
    owner_id: str | None = None
    handlers: dict[str, CommandHandler] = field(default_factory=dict)

    def register(self, name: str, handler: CommandHandler) -> None:
        self.handlers[name] = handler

    def dispatch(self, chat_id: str, text: str, *, from_id: str) -> str:
        owner = self.owner_id or self.chat_id
        if str(chat_id) != str(self.chat_id) or str(from_id) != str(owner):
            log.warning("telegram.unauthorised", chat_id=str(chat_id)[:8], from_id=str(from_id)[:8])
            return "unauthorised"
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return "not a command"
        name = parts[0][1:].split("@")[0]
        handler = self.handlers.get(name)
        if handler is None:
            return f"unknown command /{name}"
        try:
            return handler(parts[1:])
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the bot
            log.error("telegram.handler_failed", command=name, error=scrub_token(exc))
            return f"/{name} failed: {type(exc).__name__}"


def build_router(
    chat_id: str,
    *,
    owner_id: str | None = None,
    status: CommandHandler,
    positions: CommandHandler,
    pnl: CommandHandler,
    halt: CommandHandler,
    resume: CommandHandler,
    close: CommandHandler,
) -> CommandRouter:
    router = CommandRouter(chat_id=chat_id, owner_id=owner_id)
    for name, handler in (
        ("status", status),
        ("positions", positions),
        ("pnl", pnl),
        ("halt", halt),
        ("resume", resume),
        ("close", close),
    ):
        router.register(name, handler)
    return router


def alert_payload(event: str, **fields: Any) -> str:
    """Uniform one-line alert for events without a dedicated method."""
    body = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{event} {body}".strip()


def diagnose(settings: Settings | None = None) -> tuple[bool, str]:
    """Send one test message and explain the failure in words, never printing the token.

    Rotating a leaked token does not fix a 403 — the two problems look identical in the log and
    have nothing to do with each other. This separates them: a 403 means the bot exists and is
    authenticated but is not allowed to message that chat, which is almost always a chat that
    never sent the bot `/start`.
    """
    s = settings or get_settings()
    if not s.telegram_bot_token.get_secret_value():
        return False, "TELEGRAM_BOT_TOKEN is not set"
    if not s.telegram_chat_id:
        return False, "TELEGRAM_CHAT_ID is not set"

    notifier = Notifier.from_settings(s)
    try:
        notifier.bot.send(s.telegram_chat_id, "ARGUS: alert channel test.")  # type: ignore[union-attr]
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        chat = s.telegram_chat_id
        if code == 403:
            return False, (
                f"403 from Telegram for chat {chat}. The token works; the bot is not allowed to "
                "message that chat. Open Telegram, find your bot, and send it /start — a bot "
                "cannot open a conversation. If the chat is a group, add the bot to it. If the "
                "id is wrong, message @userinfobot to get yours."
            )
        if code == 401:
            return False, "401: the token is wrong or was revoked. Re-issue it with @BotFather."
        if code == 400:
            return False, f"400: Telegram rejected chat id {chat!r} as malformed."
        return False, f"HTTP {code} from Telegram."
    except Exception as exc:  # noqa: BLE001 - a diagnosis never raises
        return False, scrub_token(exc)
    return True, f"delivered to chat {s.telegram_chat_id}"
