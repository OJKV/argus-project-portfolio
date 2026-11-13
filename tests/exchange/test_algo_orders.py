"""Review 2026-09-18 X-1: a partial algo-order list is worse than no list at all.

`fetch_algo_orders` queried four `ordType` values and swallowed per-type failures. One 429 on the
`conditional` query made a protected position look unprotected: the watchdog then "repaired" it by
amending the real stop — which it found on the retry — out to 2xATR from entry. A long at 100 with
its stop trailed up to 103 came back at ~92, and Telegram reported success.
"""

from __future__ import annotations

from typing import Any

import pytest

from argus.exchange.okx import ExchangeResponseError, collect_algo_orders

LIVE_SL = {"algoId": "a1", "instId": "BTC-USDT-SWAP", "slTriggerPx": "103", "state": "live"}


def test_every_order_type_is_queried_and_merged() -> None:
    seen: list[str] = []

    def fetch(order_type: str) -> list[dict[str, Any]]:
        seen.append(order_type)
        return [LIVE_SL] if order_type == "conditional" else []

    rows = collect_algo_orders(fetch)
    assert len(seen) == 4
    assert [r["algoId"] for r in rows] == ["a1"]


def test_one_failed_query_raises_instead_of_returning_a_short_list() -> None:
    def fetch(order_type: str) -> list[dict[str, Any]]:
        if order_type == "conditional":
            raise TimeoutError("429 from OKX")
        return []

    with pytest.raises(ExchangeResponseError) as exc:
        collect_algo_orders(fetch)
    assert "conditional" in str(exc.value)
