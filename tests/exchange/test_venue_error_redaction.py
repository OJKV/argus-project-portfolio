"""Review 2026-09-18 S-3 (exchange sinks): venue error text is not safe to store or send.

ccxt puts the whole OKX response body in its exception message. On a 50110 that body names the
VPS's IP address; on a network error the message carries the request URL. Both went verbatim into
the hash-chained `position_events.reason` — which can never be edited afterwards — and into
Telegram.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.config import Settings
from argus.exchange.errors import venue_error
from argus.exchange.fake import FakeExchange
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import Watchdog

IP4 = "203.0.113.7"
IP6 = "2a01:4f9:c012:1d2e::1"
OKX_50110 = (
    'okx {"code":"50110","data":[],"msg":"Your IP '
    + IP4
    + " / "
    + IP6
    + " is not included in your API key's IP whitelist.\"}"
)
URL_ERR = "okx GET https://www.okx.com/api/v5/trade/order?instId=BTC-USDT-SWAP&ordId=1 timed out"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def test_addresses_and_query_strings_are_removed_but_the_code_survives() -> None:
    text_ = venue_error(RuntimeError(OKX_50110))
    assert IP4 not in text_ and IP6 not in text_
    assert "50110" in text_ and text_.startswith("RuntimeError: ")
    url = venue_error(TimeoutError(URL_ERR))
    assert "ordId=1" not in url and "instId=" not in url
    assert "timed out" in url


def test_timestamps_are_not_mistaken_for_ipv6() -> None:
    assert "2026-10-07T12:00:00" in venue_error(ValueError("at 2026-10-07T12:00:00 the venue"))


def test_long_bodies_are_cut_short() -> None:
    assert len(venue_error(RuntimeError("x" * 5000))) <= 300


def _reasons(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [str(r) for r in conn.execute(text("SELECT reason FROM position_events")).scalars()]


def test_a_failed_repair_stores_and_sends_no_address(engine: Engine, tmp_path: Path) -> None:
    ex = FakeExchange()
    ex.open_unprotected("BTC-USDT-SWAP")

    def refuse(symbol: str, stop: Decimal) -> str:
        raise RuntimeError(OKX_50110)

    ex.place_stop = refuse  # type: ignore[method-assign]
    bot = RecordingBot()
    with start_run(engine, "watchdog", "demo") as run_id:
        Watchdog(
            exchange=ex,
            engine=engine,
            notifier=Notifier(bot=bot, chat_id="42"),
            settings=Settings(mode="demo", argus_home=tmp_path),
        ).run_once(run_id)
    stored = " ".join(_reasons(engine))
    sent = " ".join(t for _, t in bot.sent)
    assert "repair failed" in stored
    assert IP4 not in stored and IP6 not in stored
    assert IP4 not in sent and IP6 not in sent
