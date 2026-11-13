"""Venue error text that is safe to journal and to send (review 2026-09-18 S-3).

ccxt puts the whole OKX response body in its exception message, and a network error carries the
request URL. On a 50110 the body names the VPS's IP address (IPv4 and, since F-060, IPv6). That
text went verbatim into `position_events.reason` — a hash-chained ledger row that can never be
edited afterwards — and into Telegram. Every sink that stores or sends an exchange error goes
through `venue_error` instead of interpolating the exception.

What survives is what an operator needs: the exception type, OKX's numeric code and message,
and the first few hundred characters.
"""

from __future__ import annotations

import re

import ccxt

MAX_LEN = 300


class TrimTooSmallError(ValueError):
    """A trim smaller than the venue's minimum size. A fact about the position, not a failure:
    the Reviewer may ask to trim a minimum-size position, and it simply stays as it is."""


class InstrumentNotListedError(ValueError):
    """The venue the adapter points at does not list this instrument.

    A fact about the venue, not a failure: OKX's demo environment lists fewer swaps than live
    (no ZEC, HYPE or UNI on 2026-10-07). A ValueError subclass so existing handlers still catch
    it; the manager files it as a skip rather than an error that fails the run.
    """


_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Three or more colon-separated hex groups, or any `::` compression. Timestamps (`12:00:00`) have
# only two colons and digits, so they are left alone.
_IPV6 = re.compile(
    r"(?<![\w:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{0,4}(?![\w:])"
    r"|(?<![\w:])(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?(?![\w:])",
    re.IGNORECASE,
)
_QUERY = re.compile(r"[?&][^\s\"']*")
_TELEGRAM = re.compile(r"(api\.telegram\.org/bot)[^/\s'\"]+", re.IGNORECASE)
_HEADER = re.compile(r"(OK-ACCESS-(?:KEY|SIGN|PASSPHRASE)['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+", re.I)


def redact(text: str) -> str:
    """The same text with addresses, query strings and credentials removed, then truncated."""
    out = _TELEGRAM.sub(r"\1<redacted>", text)
    out = _HEADER.sub(r"\1<redacted>", out)
    out = _QUERY.sub("", out)
    out = _IPV4.sub("<ip>", out)
    out = _IPV6.sub("<ip>", out)
    return out if len(out) <= MAX_LEN else out[: MAX_LEN - 1] + "…"


def venue_error(exc: BaseException) -> str:
    """`Type: message`, redacted. Use this wherever an exchange exception is stored or sent."""
    return redact(f"{type(exc).__name__}: {exc}")


def outcome_unknown(exc: BaseException) -> bool:
    """Could the venue have acted on a request that raised this? (review X-N5)

    A network failure — a timeout, a dropped connection, a 5xx or maintenance page — says
    nothing about whether the request arrived: the reply may simply have been lost. Anything
    else (OKX answering with an error code, our own validation) is a definite refusal.
    """
    return isinstance(exc, (ccxt.NetworkError, TimeoutError, ConnectionError))
