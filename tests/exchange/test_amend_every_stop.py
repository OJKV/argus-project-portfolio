"""`amend_stop` moves every closing-side stop on the position, or none of them (review X-12).

In net mode a position built from two entries carries one attached SL per entry. The amend
moved the first SL it found and returned, so the other one — possibly the wider — stayed live
at its old level while the caller was told the stop had moved. Now every closing-side SL is
amended together; a level that would widen any of them is refused before anything is sent, and
an amend the venue refuses for any one of them is an error even when the others went through.

The OKX half stubs ccxt and asserts on what would have been sent; nothing reaches the network.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from argus.exchange.fake import FakeExchange
from argus.exchange.okx import ExchangeResponseError, OKXAdapter, StopWideningError

SYMBOL = "BTC/USDT:USDT"
INST = "BTC-USDT-SWAP"


class _StubClient:
    def __init__(self, refuse: frozenset[str] = frozenset()) -> None:
        self.refuse = refuse
        self.amended: list[dict[str, Any]] = []
        self.markets = {SYMBOL: {"id": INST, "symbol": SYMBOL}}

    def private_post_trade_amend_algos(self, body: dict[str, Any]) -> dict[str, Any]:
        self.amended.append(body)
        code = "51000" if body["algoId"] in self.refuse else "0"
        return {"code": "0", "data": [{"algoId": body["algoId"], "sCode": code, "sMsg": ""}]}

    def private_post_trade_order_algo(self, body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("an amend over live stops must not place a new one")


def _sl(algo_id: str, trigger: str, side: str | None) -> dict[str, Any]:
    return {
        "algo_id": algo_id,
        "symbol": INST,
        "kind": "sl",
        "trigger_px": Decimal(trigger),
        "side": side,
    }


def _okx(
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    algos: list[dict[str, Any]],
    refuse: frozenset[str] = frozenset(),
) -> OKXAdapter:
    adapter = OKXAdapter.__new__(OKXAdapter)  # no network, no credentials
    adapter.client = _StubClient(refuse)  # type: ignore[assignment]
    adapter._markets_loaded = True
    monkeypatch.setattr(OKXAdapter, "price_to_precision", lambda _s, _sym, v: v)
    rows = [{"symbol": SYMBOL, "side": side, "size": Decimal("2")}]
    monkeypatch.setattr(OKXAdapter, "fetch_positions", lambda _s: rows)
    monkeypatch.setattr(OKXAdapter, "fetch_algo_orders", lambda _s: algos)
    return adapter


def _sent(adapter: OKXAdapter) -> list[tuple[str, str]]:
    bodies = adapter.client.amended  # type: ignore[attr-defined]
    return [(b["algoId"], b["newSlTriggerPx"]) for b in bodies]


@pytest.mark.parametrize(
    ("side", "close", "levels", "tighter"),
    [
        ("long", "sell", ("98000", "97000"), "99000"),
        ("short", "buy", ("102000", "103000"), "101000"),
    ],
)
def test_okx_moves_every_closing_side_stop(
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    close: str,
    levels: tuple[str, str],
    tighter: str,
) -> None:
    adapter = _okx(monkeypatch, side, [_sl("sl-1", levels[0], close), _sl("sl-2", levels[1], None)])
    adapter.amend_stop(SYMBOL, Decimal(tighter))
    assert _sent(adapter) == [("sl-1", tighter), ("sl-2", tighter)], (
        "the second entry's stop stayed at its old, wider level"
    )


@pytest.mark.parametrize(
    ("side", "close", "levels", "worse"),
    [
        ("long", "sell", ("98000", "99500"), "97000"),
        ("short", "buy", ("102000", "100500"), "103000"),
    ],
)
def test_okx_refuses_a_level_that_widens_every_live_stop(
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    close: str,
    levels: tuple[str, str],
    worse: str,
) -> None:
    """A level that moves no stop is refused, and nothing is sent."""
    adapter = _okx(
        monkeypatch, side, [_sl("sl-1", levels[0], close), _sl("sl-2", levels[1], close)]
    )
    with pytest.raises(StopWideningError):
        adapter.amend_stop(SYMBOL, Decimal(worse))
    assert _sent(adapter) == []


def test_okx_leaves_an_opening_side_algo_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """An SL-type algo on the BUY side of a long adds to it; it is not this position's stop."""
    adapter = _okx(
        monkeypatch, "long", [_sl("sl-1", "98000", "sell"), _sl("entry-1", "99500", "buy")]
    )
    adapter.amend_stop(SYMBOL, Decimal("99000"))
    assert _sent(adapter) == [("sl-1", "99000")]


def test_okx_reports_a_partly_refused_amend_after_trying_every_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first refusal must not stop the rest from tightening, nor read as success."""
    adapter = _okx(
        monkeypatch,
        "long",
        [_sl("sl-1", "98000", "sell"), _sl("sl-2", "97000", "sell")],
        refuse=frozenset({"sl-1"}),
    )
    with pytest.raises(ExchangeResponseError, match="sl-1"):
        adapter.amend_stop(SYMBOL, Decimal("99000"))
    assert _sent(adapter) == [("sl-1", "99000"), ("sl-2", "99000")]


# --- the fake must do the same, or tests pass that production would fail -------------------


def _fake_with_two_stops(side: str, first: str, second: str) -> FakeExchange:
    ex = FakeExchange()
    ex.open_unprotected(INST, side=side, size="2")
    ex.place_stop(INST, Decimal(first))
    ex.place_stop(INST, Decimal(second))
    return ex


def _levels(ex: FakeExchange) -> list[Decimal]:
    return [a["trigger_px"] for a in ex.fetch_algo_orders()]


def test_the_fake_moves_every_closing_side_stop() -> None:
    ex = _fake_with_two_stops("long", "70000", "69000")
    ex.amend_stop(INST, Decimal("71000"))
    assert _levels(ex) == [Decimal("71000"), Decimal("71000")]


def test_the_fake_refuses_a_level_that_widens_every_live_stop() -> None:
    ex = _fake_with_two_stops("long", "70000", "72000")
    with pytest.raises(StopWideningError):
        ex.amend_stop(INST, Decimal("69000"))
    assert _levels(ex) == [Decimal("70000"), Decimal("72000")]


def test_okx_retry_finishes_a_partly_moved_amend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop already at the new level is skipped, so a retry moves only the one left behind."""
    adapter = _okx(
        monkeypatch, "long", [_sl("sl-1", "97000", "sell"), _sl("sl-2", "95000", "sell")]
    )
    adapter.amend_stop(SYMBOL, Decimal("97000"))
    assert _sent(adapter) == [("sl-2", "97000")]


def test_okx_skips_a_stop_already_tighter_and_moves_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _okx(
        monkeypatch, "long", [_sl("sl-1", "98000", "sell"), _sl("sl-2", "99500", "sell")]
    )
    adapter.amend_stop(SYMBOL, Decimal("99000"))
    assert _sent(adapter) == [("sl-1", "99000")]


def test_the_fake_skips_a_stop_already_tighter() -> None:
    ex = _fake_with_two_stops("long", "70000", "72000")
    ex.amend_stop(INST, Decimal("71000"))
    assert sorted(_levels(ex)) == [Decimal("71000"), Decimal("72000")]
