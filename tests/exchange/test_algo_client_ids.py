"""Every algo ARGUS places carries an `algoClOrdId`, so its venue row can be matched (X-12).

Without one, a stop on OKX said nothing about who placed it: an ARGUS repair stop, the stop
attached to a given entry, and a stop owner set by hand in the app were indistinguishable rows.

* attached to an entry: derived from the entry's clOrdId, so a retried bracket carries the same
  ids and an algo row names the entry it belongs to;
* standalone (`place_stop`, `place_trailing`): unique per call — OKX refuses an id already in use
  (51068), and a refused repair stop is a naked position.

OKX allows 1..32 letters and digits. The OKX half stubs ccxt; nothing reaches the network.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from argus.exchange.fake import FakeExchange
from argus.exchange.okx import OKXAdapter, algo_client_id, build_bracket_params

SYMBOL = "BTC/USDT:USDT"
INST = "BTC-USDT-SWAP"
OKX_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")


class _StubClient:
    def __init__(self, pending: list[dict[str, Any]] | None = None) -> None:
        self.placed: list[dict[str, Any]] = []
        self.pending = pending or []
        self.markets = {SYMBOL: {"id": INST, "symbol": SYMBOL}}

    def private_post_trade_order_algo(self, body: dict[str, Any]) -> dict[str, Any]:
        self.placed.append(body)
        return {"code": "0", "data": [{"algoId": f"n{len(self.placed)}", "sCode": "0"}]}

    def private_get_trade_orders_algo_pending(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = self.pending if params["ordType"] == "conditional" else []
        return {"code": "0", "data": rows}


def _okx(monkeypatch: pytest.MonkeyPatch, pending: list[dict[str, Any]] | None = None) -> Any:
    adapter = OKXAdapter.__new__(OKXAdapter)  # no network, no credentials
    adapter.client = _StubClient(pending)  # type: ignore[assignment]
    adapter._markets_loaded = True
    monkeypatch.setattr(OKXAdapter, "price_to_precision", lambda _s, _sym, v: v)
    rows = [{"symbol": SYMBOL, "side": "long", "size": Decimal("1")}]
    monkeypatch.setattr(OKXAdapter, "fetch_positions", lambda _s: rows)
    return adapter


def test_standalone_algos_are_tagged_and_never_reuse_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _okx(monkeypatch)
    adapter.place_stop(SYMBOL, Decimal("95000"))
    adapter.place_stop(SYMBOL, Decimal("95000"))
    adapter.place_trailing(SYMBOL, Decimal("2"), None)
    ids = [body.get("algoClOrdId") for body in adapter.client.placed]
    assert all(isinstance(i, str) and OKX_ID.match(i) for i in ids), ids
    assert len(set(ids)) == 3, "a reused algoClOrdId is refused by OKX (51068)"


def test_attached_algos_are_tagged_from_the_entry_id() -> None:
    order: dict[str, Any] = {
        "cl_ord_id": "argus0123456789abcdef0123456789a",
        "attach_algos": [
            {"kind": "sl", "trigger_px": Decimal("72000")},
            {"kind": "tp", "trigger_px": Decimal("80000"), "size": Decimal("0.5")},
            {"kind": "tp", "trigger_px": Decimal("84000"), "size": Decimal("0.5")},
        ],
    }
    attached = build_bracket_params(order)["attachAlgoOrds"]
    ids = [a.get("attachAlgoClOrdId") for a in attached]
    assert all(isinstance(i, str) and OKX_ID.match(i) for i in ids), ids
    assert len(set(ids)) == 3, "two split targets must not share an id"
    again = [a["attachAlgoClOrdId"] for a in build_bracket_params(order)["attachAlgoOrds"]]
    assert again == ids, "a retried bracket must carry the same ids"
    assert ids[0] == algo_client_id("sl", parent=order["cl_ord_id"], index=0)


def test_the_venue_id_comes_back_on_the_algo_row(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "algoId": "a1",
        "algoClOrdId": "argussl0123",
        "instId": INST,
        "slTriggerPx": "95000",
        "side": "sell",
        "state": "live",
    }
    adapter = _okx(monkeypatch, pending=[row])
    [algo] = adapter.fetch_algo_orders()
    assert algo["algo_cl_ord_id"] == "argussl0123"


def test_the_fake_tags_its_algos_the_same_way() -> None:
    ex = FakeExchange()
    ex.place_bracket(
        {
            "symbol": INST,
            "side": "long",
            "cl_ord_id": "c1",
            "size": "1",
            "price": "75000",
            "attach_algos": [{"kind": "sl", "trigger_px": "72000"}],
        }
    )
    rows = ex.fetch_algo_orders()
    assert rows[0]["algo_cl_ord_id"] == algo_client_id("sl", parent="c1", index=0)
    ex.open_unprotected(INST)
    ex.place_stop(INST, Decimal("70000"))
    ex.place_stop(INST, Decimal("70000"))
    standalone = [r["algo_cl_ord_id"] for r in ex.fetch_algo_orders()][1:]
    assert all(OKX_ID.match(i) for i in standalone)
    assert len(set(standalone)) == 2
