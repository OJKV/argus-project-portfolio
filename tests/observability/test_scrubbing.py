"""Review 2026-09-18 S-2: telemetry must never carry a credential off the box.

`capture_headers=False` stops header capture, but OpenTelemetry records `url.full` on every httpx
span and Logfire's scrubber treats URL attributes as safe. Telegram puts the bot token in the URL
*path*; FRED and Coinalyze put `api_key=` in the *query*; healthchecks.io is a secret URL.
"""

from __future__ import annotations

import pytest

from argus.observability.setup import scrub_url


@pytest.mark.parametrize(
    ("url", "must_not_contain"),
    [
        ("https://api.telegram.org/bot8123456:AAH-SECRET-TOKEN/sendMessage", "AAH-SECRET-TOKEN"),
        (
            "https://api.stlouisfed.org/fred/series?series_id=DFF&api_key=abcdef123456",
            "abcdef123456",
        ),
        ("https://api.coinalyze.net/v1/exchanges?api_key=deadbeef", "deadbeef"),
        ("https://hc-ping.com/6f1e4c6a-0000-1111-2222-333344445555", "6f1e4c6a"),
        ("https://api.coingecko.com/api/v3/simple/price?x_cg_demo_api_key=CG-abc", "CG-abc"),
    ],
)
def test_no_secret_survives_scrubbing(url: str, must_not_contain: str) -> None:
    assert must_not_contain not in scrub_url(url)


def test_the_useful_part_of_the_url_survives() -> None:
    scrubbed = scrub_url("https://api.stlouisfed.org/fred/series?series_id=DFF&api_key=secret")
    assert "api.stlouisfed.org" in scrubbed
    assert "/fred/series" in scrubbed


def test_telegram_path_keeps_the_method_name() -> None:
    assert scrub_url("https://api.telegram.org/bot123:ABC/sendMessage").endswith("/sendMessage")


def test_a_url_without_secrets_is_left_alone() -> None:
    plain = "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"
    assert scrub_url(plain) == plain
