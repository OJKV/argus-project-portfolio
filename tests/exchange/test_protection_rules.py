"""Review 2026-09-18 X-9: "protected" accepted any SL or trailing algo on the instrument.

No check of the algo's side against the position, of its size against the position, or of
whether a trailing stop had activated. An unactivated `move_order_stop` is zero downside
protection; an SL sized for half the position leaves half naked; a buy-side "stop" on a long
would add to it. All three counted as protected, so the watchdog never repaired them. Orphan
algos — protection left on an instrument with no position — were never reported.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from argus.exchange.fake import FakeExchange
from argus.exchange.okx import OKXAdapter, is_protected

LONG = {"symbol": "BTC/USDT:USDT", "side": "long", "size": Decimal("2")}


def _algo(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "sl",
        "side": "sell",
        "size": Decimal("2"),
        "close_fraction": None,
        "active_px": None,
        "trigger_px": Decimal("70000"),
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("algo", "protected"),
    [
        (_algo(), True),
        (_algo(side="buy"), False),  # would add to the long, not close it
        (_algo(size=Decimal("1")), False),  # half the position naked
        (_algo(size=Decimal("1"), close_fraction="1"), True),  # closes the whole position
        (_algo(size=Decimal("0")), True),  # size not reported (attached): not under-sized
        (_algo(side=None), True),  # side not reported: not provably wrong
        (_algo(kind="trailing", trigger_px=None), True),  # trails from placement
        (_algo(kind="trailing", active_px=Decimal("77000")), False),  # may not have activated
        (_algo(kind="tp"), False),
    ],
)
def test_what_counts_as_a_stop(algo: dict[str, Any], protected: bool) -> None:
    assert is_protected(LONG, [algo]) is protected


def test_a_short_needs_a_buy_side_stop() -> None:
    short = {**LONG, "side": "short"}
    assert is_protected(short, [_algo(side="buy")])
    assert not is_protected(short, [_algo(side="sell")])


class _Venue:
    def __init__(self, positions: list[dict[str, Any]], algos: list[dict[str, Any]]) -> None:
        self.positions, self.algos = positions, algos


def test_okx_reconcile_applies_the_rules_and_reports_orphan_algos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = OKXAdapter.__new__(OKXAdapter)
    positions = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "size": Decimal("2")},
        {"symbol": "ETH/USDT:USDT", "side": "long", "size": Decimal("2")},
    ]
    algos = [
        {**_algo(), "symbol": "BTC-USDT-SWAP"},
        {**_algo(size=Decimal("1")), "symbol": "ETH-USDT-SWAP"},  # under-sized
        {**_algo(), "symbol": "SOL-USDT-SWAP", "algo_id": "a9"},  # no SOL position
    ]
    monkeypatch.setattr(OKXAdapter, "fetch_positions", lambda _s: positions)
    monkeypatch.setattr(OKXAdapter, "fetch_algo_orders", lambda _s: algos)
    monkeypatch.setattr(OKXAdapter, "fetch_open_orders", lambda _s: [])
    monkeypatch.setattr(
        OKXAdapter, "_market", lambda _s, sym: {"id": sym.split("/")[0] + "-USDT-SWAP"}
    )
    state = adapter.reconcile()
    assert state["unprotected"] == ["ETH/USDT:USDT"]
    assert [a["symbol"] for a in state["orphan_algos"]] == ["SOL-USDT-SWAP"]


def test_the_fake_applies_the_same_rules() -> None:
    """X-16: a fake more lenient than the venue hides exactly the defects it exists to catch."""
    ex = FakeExchange()
    ex.open_unprotected("BTC-USDT-SWAP", size="2")
    ex.place_trailing("BTC-USDT-SWAP", callback_pct=Decimal("2"), activation=Decimal("77000"))
    assert ex.reconcile()["unprotected"] == ["BTC-USDT-SWAP"]


def test_partial_stops_that_together_cover_the_position_protect_it() -> None:
    """Review 2026-10-07 (exec): each SL had to cover the whole position on its own.

    In net mode a second same-side entry adds to the position and brings its own attached SL,
    sized for that entry. Two SLs of 5 on a position of 10 cover it; calling that naked sent the
    watchdog to "repair" it every tick.
    """
    long10 = {**LONG, "size": Decimal("10")}
    halves = [
        _algo(size=Decimal("5"), trigger_px=Decimal("97000")),
        _algo(size=Decimal("5"), trigger_px=Decimal("96500")),
    ]
    assert is_protected(long10, halves)
    assert not is_protected(long10, halves[:1])
    assert not is_protected(long10, [halves[0], _algo(size=Decimal("4"))])
    # A stop on the wrong side adds to the position; it covers none of it.
    assert not is_protected(long10, [halves[0], _algo(size=Decimal("5"), side="buy")])
