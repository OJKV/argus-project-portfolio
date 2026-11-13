"""What a closed position was worth, as the venue booked it - not the last unrealised we saw.

The watchdog snapshots positions every 20 minutes. A long seen at +0.1R that stops out at -1R
before the next tick was booked as a +0.1R win: the loss limits read it short, the losing streak
broke, and the stop-out cooldown never saw it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from argus.exchange.okx import ExchangeResponseError, realised_from_history

SINCE = datetime(2026, 9, 25, 12, tzinfo=UTC)
MS = int(SINCE.timestamp() * 1000)


def _row(pnl: str, at_ms: int, inst: str = "SOL-USDT-SWAP") -> dict[str, str]:
    return {"instId": inst, "realizedPnl": pnl, "uTime": str(at_ms), "type": "2"}


def test_the_close_after_the_last_look_is_read_from_positions_history() -> None:
    rows = [_row("-40.25", MS + 600_000), _row("12", MS - 3_600_000)]
    assert realised_from_history(rows, "SOL-USDT-SWAP", SINCE) == Decimal("-40.25")


def test_closes_after_the_last_look_are_summed() -> None:
    """Closed and reopened between two ticks: both closes are this account's PnL."""
    rows = [_row("-10", MS + 60_000), _row("-5", MS + 120_000)]
    assert realised_from_history(rows, "SOL-USDT-SWAP", SINCE) == Decimal("-15")


def test_no_close_on_record_is_none_not_zero() -> None:
    rows = [_row("12", MS - 1), _row("-3", MS + 60_000, inst="ETH-USDT-SWAP")]
    assert realised_from_history(rows, "SOL-USDT-SWAP", SINCE) is None


@pytest.mark.parametrize("bad", ["", "NaN", "abc"])
def test_an_unreadable_figure_is_refused(bad: str) -> None:
    with pytest.raises(ExchangeResponseError):
        realised_from_history([_row(bad, MS + 60_000)], "SOL-USDT-SWAP", SINCE)
