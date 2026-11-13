"""`amend_stop` only ever moves a live stop towards profit — in the adapter itself.

Review 2026-10-07 R-1 / X-1. The Reviewer's tighten-only check lived in `runs/review.py` and the
adapter amended whatever it was handed. The watchdog calls the same method, so one caller with a
wrong side or a stale number was enough to move a stop away from price. The venue call is the
last place the check can live where every caller passes through it, so it lives there too.

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
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.amended: list[dict[str, Any]] = []
        self.placed: list[dict[str, Any]] = []
        self.markets = {SYMBOL: {"id": INST, "symbol": SYMBOL}}

    def private_post_trade_amend_algos(self, body: dict[str, Any]) -> dict[str, Any]:
        self.amended.append(body)
        return self.response

    def private_post_trade_order_algo(self, body: dict[str, Any]) -> dict[str, Any]:
        self.placed.append(body)
        return {"code": "0", "data": [{"algoId": "new-1", "sCode": "0", "sMsg": ""}]}


OK = {"code": "0", "data": [{"algoId": "sl-1", "sCode": "0", "sMsg": ""}]}


def _okx(
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    stop: str | None,
    response: dict[str, Any] | None = None,
    positions: bool = True,
) -> OKXAdapter:
    adapter = OKXAdapter.__new__(OKXAdapter)  # no network, no credentials
    adapter.client = _StubClient(response or OK)  # type: ignore[assignment]
    adapter._markets_loaded = True
    monkeypatch.setattr(OKXAdapter, "price_to_precision", lambda _s, _sym, v: v)
    rows = [{"symbol": SYMBOL, "side": side, "size": Decimal("1")}] if positions else []
    monkeypatch.setattr(OKXAdapter, "fetch_positions", lambda _s: rows)
    algos = (
        []
        if stop is None
        else [
            {
                "algo_id": "sl-1",
                "symbol": INST,
                "kind": "sl",
                "trigger_px": Decimal(stop),
            }
        ]
    )
    monkeypatch.setattr(OKXAdapter, "fetch_algo_orders", lambda _s: algos)
    return adapter


def _sent(adapter: OKXAdapter) -> list[dict[str, Any]]:
    return adapter.client.amended  # type: ignore[attr-defined, no-any-return]


@pytest.mark.parametrize(
    ("side", "current", "wider"), [("long", "98000", "95000"), ("short", "102000", "105000")]
)
def test_okx_refuses_to_widen_a_live_stop(
    monkeypatch: pytest.MonkeyPatch, side: str, current: str, wider: str
) -> None:
    adapter = _okx(monkeypatch, side, current)
    with pytest.raises(StopWideningError):
        adapter.amend_stop(SYMBOL, Decimal(wider))
    assert _sent(adapter) == [], "a widening must never reach the venue"


@pytest.mark.parametrize(("side", "current"), [("long", "98000"), ("short", "102000")])
def test_okx_refuses_an_amend_that_does_not_move_the_stop(
    monkeypatch: pytest.MonkeyPatch, side: str, current: str
) -> None:
    adapter = _okx(monkeypatch, side, current)
    with pytest.raises(StopWideningError):
        adapter.amend_stop(SYMBOL, Decimal(current))
    assert _sent(adapter) == []


@pytest.mark.parametrize(
    ("side", "current", "tighter"), [("long", "98000", "99000"), ("short", "102000", "101000")]
)
def test_okx_tightens_a_live_stop(
    monkeypatch: pytest.MonkeyPatch, side: str, current: str, tighter: str
) -> None:
    adapter = _okx(monkeypatch, side, current)
    assert adapter.amend_stop(SYMBOL, Decimal(tighter)) == "sl-1"
    assert _sent(adapter) == [{"instId": INST, "algoId": "sl-1", "newSlTriggerPx": tighter}]


@pytest.mark.parametrize("unreadable", ["0", "-1"])
def test_okx_refuses_to_amend_a_stop_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, unreadable: str
) -> None:
    """A stop whose level is unknown cannot be shown to be tightened by anything."""
    adapter = _okx(monkeypatch, "long", unreadable)
    with pytest.raises(StopWideningError):
        adapter.amend_stop(SYMBOL, Decimal("99000"))
    assert _sent(adapter) == []


@pytest.mark.parametrize(
    "response",
    [
        {"code": "0", "data": [{"algoId": "sl-1", "sCode": "51000", "sMsg": "Parameter error"}]},
        {"code": "0", "data": []},
        {"code": "0"},
    ],
)
def test_okx_reports_an_amend_the_venue_did_not_confirm(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    """The amend response was discarded (X-12): a rejected amend read as a moved stop."""
    adapter = _okx(monkeypatch, "long", "98000", response=response)
    with pytest.raises(ExchangeResponseError):
        adapter.amend_stop(SYMBOL, Decimal("99000"))


def test_okx_refuses_to_amend_with_no_position_to_read_the_side_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _okx(monkeypatch, "long", "98000", positions=False)
    with pytest.raises(ExchangeResponseError):
        adapter.amend_stop(SYMBOL, Decimal("99000"))
    assert _sent(adapter) == []


def test_okx_still_places_a_stop_where_none_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog's repair path: nothing to widen, so nothing to refuse."""
    adapter = _okx(monkeypatch, "long", None)
    assert adapter.amend_stop(SYMBOL, Decimal("95000")) == "new-1"
    placed = adapter.client.placed  # type: ignore[attr-defined]
    assert placed and placed[0]["slTriggerPx"] == "95000" and placed[0]["side"] == "sell"


# --- the fake must refuse the same things, or tests pass that production would fail ----------


@pytest.mark.parametrize(
    ("side", "current", "wider"),
    [("long", "70000", "69000"), ("short", "80000", "81000"), ("sell", "80000", "81000")],
)
def test_the_fake_refuses_to_widen_a_live_stop(side: str, current: str, wider: str) -> None:
    """`sell` too: a filled fake position carries the order vocabulary (X-N8)."""
    ex = FakeExchange()
    ex.open_unprotected(INST, side=side)
    ex.amend_stop(INST, Decimal(current))
    with pytest.raises(StopWideningError):
        ex.amend_stop(INST, Decimal(wider))
    assert [a["trigger_px"] for a in ex.fetch_algo_orders()] == [Decimal(current)]


@pytest.mark.parametrize(
    ("side", "current", "tighter"), [("long", "70000", "71000"), ("short", "80000", "79000")]
)
def test_the_fake_tightens_a_live_stop(side: str, current: str, tighter: str) -> None:
    ex = FakeExchange()
    ex.open_unprotected(INST, side=side)
    ex.amend_stop(INST, Decimal(current))
    ex.amend_stop(INST, Decimal(tighter))
    assert [a["trigger_px"] for a in ex.fetch_algo_orders()] == [Decimal(tighter)]


def test_the_fake_refuses_a_stop_with_no_position() -> None:
    """OKX cannot size a reduce-only stop for a position that is not there; neither can this."""
    ex = FakeExchange()
    with pytest.raises(ExchangeResponseError):
        ex.amend_stop(INST, Decimal("70000"))
    assert ex.fetch_algo_orders() == []
