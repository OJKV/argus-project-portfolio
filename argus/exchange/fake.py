"""In-memory ExchangeAdapter for tests (AGENTS.md rule 1: tests never touch a live venue).

Models the OKX behaviours the gate and watchdog depend on: net position mode, one position
per symbol, attached algo orders that survive independently of the entry order, per-instrument
precision, and responses that can be made hostile on purpose (zero-qty fills, F-030).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from argus.exchange.base import ExchangeAdapter
from argus.exchange.okx import (
    ExchangeResponseError,
    StopWideningError,
    algo_client_id,
    is_protected,
    stops_to_amend,
    venue_side,
)
from argus.exchange.reconcile import symbol_key

DEFAULT_SPECS: dict[str, dict[str, Any]] = {
    "BTC-USDT-SWAP": {
        "ct_val": Decimal("0.01"),
        "min_size": Decimal("0.01"),
        "lot": Decimal("0.01"),
        "tick": Decimal("0.1"),
        "max_leverage": 100,
    },
    "ETH-USDT-SWAP": {
        "ct_val": Decimal("0.1"),
        "min_size": Decimal("0.01"),
        "lot": Decimal("0.01"),
        "tick": Decimal("0.01"),
        "max_leverage": 100,
    },
    "SOL-USDT-SWAP": {
        "ct_val": Decimal("1"),
        "min_size": Decimal("0.01"),
        "lot": Decimal("0.01"),
        "tick": Decimal("0.001"),
        "max_leverage": 100,
    },
}


@dataclass
class FakeOrder:
    order_id: str
    cl_ord_id: str
    symbol: str
    side: str
    kind: str
    order_type: str
    size: Decimal
    price: Decimal | None
    state: str = "OPEN"
    reduce_only: bool = False
    # What OKX reports for a filled order: average price and the signed fee (X-N11, F-036).
    fill_price: Decimal | None = None
    fill_fee: Decimal | None = None


@dataclass
class FakeAlgo:
    algo_id: str
    symbol: str
    kind: str  # sl | tp | trailing
    trigger_px: Decimal | None
    size: Decimal
    callback_pct: Decimal | None = None
    state: str = "live"
    # Reported like OKX's algo rows so the same protection rules apply to both (review X-9).
    side: str | None = None
    close_fraction: str | None = None
    active_px: Decimal | None = None
    # The `algoClOrdId` OKX would report, from the same function (review X-12).
    algo_cl_ord_id: str | None = None


@dataclass
class FakePosition:
    symbol: str
    side: str
    size: Decimal
    entry_price: Decimal
    leverage: int
    plan_id: str | None = None
    # OKX's mgnRatio, carried so the watchdog's margin floor is exercised offline too (X-7).
    margin_ratio: Decimal | None = None


@dataclass
class FakeExchange(ExchangeAdapter):
    """Deterministic venue. Nothing here reaches the network."""

    name: str = "fake"
    equity: Decimal = Decimal("1000")
    marks: dict[str, Decimal] = field(default_factory=lambda: {"BTC-USDT-SWAP": Decimal("75000")})
    specs: dict[str, dict[str, Any]] = field(default_factory=lambda: dict(DEFAULT_SPECS))
    positions: dict[str, FakePosition] = field(default_factory=dict)
    orders: dict[str, FakeOrder] = field(default_factory=dict)
    algos: dict[str, FakeAlgo] = field(default_factory=dict)
    leverage: dict[str, int] = field(default_factory=dict)
    maker_fee: Decimal = Decimal("-0.0002")
    taker_fee: Decimal = Decimal("-0.0005")
    # What positions-history would say about a closed symbol; tests set it.
    realised: dict[str, Decimal] = field(default_factory=dict)
    # failure injection
    fail_next: Exception | None = None
    # "Timeout, but accepted": place_bracket records the order and its algos, then raises this —
    # the reply lost on the way back while the venue holds a live order (review X-N5).
    accept_then_fail: Exception | None = None
    zero_qty_fill: bool = False
    _seq: int = 0

    # --- helpers ---------------------------------------------------------
    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:04d}"

    def _check(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    @staticmethod
    def _okx_id(symbol: str) -> str:
        """Accept ccxt's spelling ("BTC/USDT:USDT", what the real adapter resolves) as well as
        OKX's id ("BTC-USDT-SWAP"), which most tests use. The real contract is pinned by
        `test_the_manager_hands_the_order_path_a_symbol_the_okx_adapter_can_resolve` (F-162)."""
        if "/" in symbol:
            return f"{symbol.split('/')[0]}-USDT-SWAP"
        return symbol

    def _mark(self, symbol: str, default: Decimal) -> Decimal:
        return self.marks.get(symbol, self.marks.get(self._okx_id(symbol), default))

    def _spec(self, symbol: str) -> dict[str, Any]:
        key = symbol if symbol in self.specs else self._okx_id(symbol)
        if key not in self.specs:
            raise ValueError(f"unknown instrument {symbol}")
        return self.specs[key]

    # --- data ------------------------------------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        self._check()
        mark = float(self._mark(symbol, Decimal("100")))
        return [[float(i), mark, mark * 1.01, mark * 0.99, mark, 1000.0] for i in range(limit)]

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        self._check()
        mark = self._mark(symbol, Decimal("100"))
        return {
            "symbol": symbol,
            "last": mark,
            "bid": mark * Decimal("0.9999"),
            "ask": mark * Decimal("1.0001"),
        }

    def fetch_funding(self, symbol: str) -> dict[str, Any]:
        self._check()
        return {"symbol": symbol, "funding_rate": Decimal("0.0001"), "next_funding_time": None}

    def fetch_open_interest(self, symbol: str) -> dict[str, Any]:
        self._check()
        return {"symbol": symbol, "open_interest": Decimal("1000")}

    def instrument_spec(self, symbol: str) -> dict[str, Any]:
        return dict(self._spec(symbol))

    # --- account ---------------------------------------------------------
    def fetch_balance(self) -> dict[str, Any]:
        self._check()
        used = sum(
            (p.size * self._spec(p.symbol)["ct_val"] * p.entry_price / p.leverage)
            for p in self.positions.values()
        )
        return {"equity": self.equity, "free": self.equity - Decimal(used), "currency": "USDT"}

    def fetch_positions(self) -> list[dict[str, Any]]:
        self._check()
        return [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "entry_price": p.entry_price,
                "mark_price": self._mark(p.symbol, p.entry_price),
                "leverage": p.leverage,
                "plan_id": p.plan_id,
                "margin_ratio": p.margin_ratio,
            }
            for p in self.positions.values()
        ]

    def fetch_open_orders(self) -> list[dict[str, Any]]:
        self._check()
        # `id` as well as `order_id`: ccxt's unified order calls it `id`, and a fake that only
        # spoke its own dialect would let code pass here that KeyErrors on the real venue.
        return [
            vars(o) | {"id": o.order_id, "clientOrderId": o.cl_ord_id, "filled": Decimal("0")}
            for o in self.orders.values()
            if o.state in ("OPEN", "PARTIAL")
        ]

    def fetch_algo_orders(self) -> list[dict[str, Any]]:
        self._check()
        return [vars(a) for a in self.algos.values() if a.state == "live"]

    def fetch_realised_pnl(self, symbol: str, since: datetime) -> Decimal | None:
        self._check()
        return self.realised.get(symbol)

    def fetch_fee_rates(self, symbol: str) -> dict[str, Decimal]:
        self._check()
        return {"maker": self.maker_fee, "taker": self.taker_fee}

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._check()
        max_lev = int(self._spec(symbol)["max_leverage"])
        if not 1 <= leverage <= max_lev:
            raise ValueError(f"leverage {leverage} outside 1..{max_lev} for {symbol}")
        self.leverage[symbol] = leverage

    # --- execution -------------------------------------------------------
    def place_bracket(self, order: dict[str, Any]) -> dict[str, str]:
        """Entry order plus attached SL/TP/trailing algos, exactly as OKX attachAlgoOrds does."""
        self._check()
        symbol = order["symbol"]
        # Normalised through the SAME function the OKX adapter uses, and it raises on anything
        # else. This read `order["side"]` straight through, so it happily accepted a long that
        # had already been mistranslated into a sell — the fake agreed with the bug and only the
        # real venue disagreed (F-137). A fake more permissive than the venue hides exactly the
        # class of defect a fake exists to catch.
        side = venue_side(order["side"])
        # OKX rejects a stop on the wrong side of the entry (sCode 51049). Enforced here too, so
        # the mistake fails in a unit test rather than in a live placement.
        price = Decimal(str(order["price"])) if order.get("price") else None
        for algo in order.get("attach_algos", []):
            if algo.get("kind") != "sl" or price is None or algo.get("trigger_px") is None:
                continue
            trigger = Decimal(str(algo["trigger_px"]))
            if (side == "buy" and trigger >= price) or (side == "sell" and trigger <= price):
                raise ValueError(
                    f"stop {trigger} is on the wrong side of a {side} entry at {price}"
                )
        # Both sides normalised before comparing: positions are stored as long/short and the
        # order arrives as buy/sell, so comparing them raw would report a net-mode conflict for
        # a position on the SAME side.
        if symbol in self.positions and venue_side(self.positions[symbol].side) != side:
            raise ValueError(f"net mode: {symbol} already open on the other side")
        cl_ord_id = order["cl_ord_id"]
        existing = [o for o in self.orders.values() if o.cl_ord_id == cl_ord_id]
        if existing:  # idempotency: never double-place
            return {"entry_id": existing[0].order_id}
        size = Decimal(str(order["size"]))
        if size <= 0:
            raise ValueError("size must be > 0")
        order_id = self._next("o")
        self.orders[order_id] = FakeOrder(
            order_id=order_id,
            cl_ord_id=cl_ord_id,
            symbol=symbol,
            side=side,
            kind="entry",
            order_type=order.get("order_type", "limit"),
            size=size,
            price=price,
        )
        out = {"entry_id": order_id}
        for index, algo in enumerate(order.get("attach_algos", [])):
            algo_id = self._next("a")
            self.algos[algo_id] = FakeAlgo(
                algo_id=algo_id,
                symbol=symbol,
                kind=algo["kind"],
                trigger_px=Decimal(str(algo["trigger_px"])) if algo.get("trigger_px") else None,
                size=Decimal(str(algo.get("size", size))),
                callback_pct=(
                    Decimal(str(algo["callback_pct"])) if algo.get("callback_pct") else None
                ),
                side="sell" if side == "buy" else "buy",
                active_px=(
                    Decimal(str(algo["trigger_px"]))
                    if algo["kind"] == "trailing" and algo.get("trigger_px")
                    else None
                ),
                algo_cl_ord_id=algo_client_id(algo["kind"], cl_ord_id, index),
            )
            out[f"{algo['kind']}_algo_id"] = algo_id
        if self.accept_then_fail is not None:
            exc, self.accept_then_fail = self.accept_then_fail, None
            raise exc
        return out

    def amend_stop(self, symbol: str, new_stop: Decimal) -> str:
        """Tighten-only, side read from the position — the same refusals as OKX (review R-1).

        A fake that amended anything let the Reviewer's widening path pass its tests; one that
        placed a size-0 stop with no position hid a case OKX rejects (X-16).
        """
        self._check()
        position = self.positions.get(symbol)
        if position is None:
            raise ExchangeResponseError(f"no open position on {symbol}")
        live = [a for a in self.algos.values() if a.symbol == symbol and a.state == "live"]
        try:
            # Every closing-side stop or none, as OKX (review X-12).
            stops = stops_to_amend(position.side, new_stop, [vars(a) for a in live])
        except StopWideningError as exc:
            raise StopWideningError(f"{symbol}: {exc}") from None
        if stops:
            ids = [str(s["algo_id"]) for s in stops]
            for algo in live:
                if algo.algo_id in ids:
                    algo.trigger_px = new_stop
            return ",".join(ids)
        algo_id = self._next("a")
        self.algos[algo_id] = FakeAlgo(
            algo_id, symbol, "sl", new_stop, position.size, algo_cl_ord_id=algo_client_id("sl")
        )
        return algo_id

    def place_stop(self, symbol: str, stop: Decimal) -> str:
        """As OKX's conditional SL with `closeFraction: "1"`: the whole position, whatever size."""
        self._check()
        algo_id = self._next("a")
        position = self.positions.get(symbol)
        self.algos[algo_id] = FakeAlgo(
            algo_id,
            symbol,
            "sl",
            stop,
            position.size if position is not None else Decimal("0"),
            side=None if position is None else ("sell" if position.side == "long" else "buy"),
            close_fraction="1",
            algo_cl_ord_id=algo_client_id("sl"),
        )
        return algo_id

    def place_trailing(self, symbol: str, callback_pct: Decimal, activation: Decimal | None) -> str:
        self._check()
        algo_id = self._next("a")
        position = self.positions.get(symbol)
        size = position.size if position is not None else Decimal("0")
        self.algos[algo_id] = FakeAlgo(
            algo_id,
            symbol,
            "trailing",
            activation,
            size,
            callback_pct=callback_pct,
            side=None if position is None else ("sell" if position.side == "long" else "buy"),
            active_px=activation,
            algo_cl_ord_id=algo_client_id("trailing"),
        )
        return algo_id

    def partial_close(self, symbol: str, pct: int) -> str:
        self._check()
        pos = self.positions[symbol]
        closing = (pos.size * Decimal(pct) / Decimal(100)).quantize(self._spec(symbol)["lot"])
        pos.size -= closing
        if pos.size <= 0:
            del self.positions[symbol]
            return self._next("o")
        # As the OKX adapter does after a trim (review X-N9): sized stops and targets larger than
        # what is left shrink to it.
        for algo in self.algos.values():
            if (
                algo.symbol == symbol
                and algo.state == "live"
                and algo.kind in ("sl", "tp")
                and algo.close_fraction != "1"
                and algo.size > pos.size
            ):
                algo.size = pos.size
        return self._next("o")

    def close(self, symbol: str) -> str:
        self._check()
        self.positions.pop(symbol, None)
        for algo in self.algos.values():
            if algo.symbol == symbol:
                algo.state = "cancelled"
        return self._next("o")

    def fetch_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        self._check()
        order = self.orders.get(order_id)
        if order is None or order.symbol != symbol:
            raise LookupError(f"no order {order_id} on {symbol}")
        status = {"OPEN": "open", "PARTIAL": "open", "FILLED": "closed"}.get(
            order.state, "canceled"
        )
        filled = order.size if order.state == "FILLED" else Decimal("0")
        out = vars(order) | {"id": order.order_id, "status": status, "filled": filled}
        if order.fill_price is not None and order.fill_fee is not None:
            # ccxt's unified shape: `average`, a positive `fee.cost`, and OKX's own signed fee
            # in `info` — the shape the resting sweep records fills from.
            out |= {
                "average": order.fill_price,
                "fee": {"cost": -order.fill_fee, "currency": "USDT"},
                "info": {"fee": str(order.fill_fee), "feeCcy": "USDT"},
            }
        return out

    def fetch_order_by_client_id(self, symbol: str, cl_ord_id: str) -> dict[str, Any] | None:
        self._check()
        for order in self.orders.values():
            if order.cl_ord_id == cl_ord_id and order.symbol == symbol:
                return self.fetch_order(symbol, order.order_id)
        return None

    def cancel_order(self, symbol: str, order_id: str) -> None:
        self._check()
        order = self.orders.get(order_id)
        if order is None or order.symbol != symbol or order.state not in ("OPEN", "PARTIAL"):
            raise ValueError(f"no resting order {order_id} on {symbol}")
        order.state = "CANCELLED"

    def cancel_all(self, symbol: str) -> None:
        self._check()
        for order in self.orders.values():
            if order.symbol == symbol and order.state in ("OPEN", "PARTIAL"):
                order.state = "CANCELLED"

    def reconcile(self) -> dict[str, Any]:
        """Exchange is truth. The same contract and protection rules as the OKX adapter.

        It used to count any live SL/trailing algo as protection and to return an `orphans` key
        the real adapter never had (review X-9, X-16). Orphan *positions* are judged against
        the journal by the watchdog; the venue only knows about orphan algos.
        """
        self._check()
        positions = self.fetch_positions()
        algos = self.fetch_algo_orders()
        held = {symbol_key(p["symbol"]) for p in positions}
        return {
            "positions": positions,
            "unprotected": [
                p["symbol"]
                for p in positions
                if not is_protected(
                    p, [a for a in algos if symbol_key(a["symbol"]) == symbol_key(p["symbol"])]
                )
            ],
            "open_orders": self.fetch_open_orders(),
            "algo_orders": algos,
            "orphan_algos": [a for a in algos if symbol_key(a["symbol"]) not in held],
        }

    # --- test helpers ----------------------------------------------------
    def fill(self, order_id: str, price: Decimal | None = None) -> dict[str, Any]:
        """Fill an entry order into a position and return the fill (F-030: qty may be zero)."""
        order = self.orders[order_id]
        size = Decimal("0") if self.zero_qty_fill else order.size
        fill_px = price or order.price or self._mark(order.symbol, Decimal("0"))
        order.state = "FILLED"
        order.fill_price = fill_px if size > 0 else None
        order.fill_fee = size * self._spec(order.symbol)["ct_val"] * fill_px * self.taker_fee
        if size > 0:
            # Orders travel as buy/sell, positions come back from OKX as long/short. Storing the
            # order's `buy` here made every consumer that asks `side == "long"` read a filled
            # long as a short — the watchdog's breach check inverted (review X-N8).
            self.positions[order.symbol] = FakePosition(
                symbol=order.symbol,
                side="long" if order.side == "buy" else "short",
                size=size,
                entry_price=fill_px,
                leverage=self.leverage.get(order.symbol, 1),
            )
        notional = size * self._spec(order.symbol)["ct_val"] * fill_px
        return {
            "order_id": order_id,
            "symbol": order.symbol,
            "side": order.side,
            "price": fill_px,
            "size": size,
            "fee": notional * self.taker_fee,
            "fee_ccy": "USDT",
        }

    def open_unprotected(self, symbol: str, side: str = "long", size: str = "1") -> None:
        """A position that exists on the venue with no stop — the watchdog's P1 case."""
        self.positions[symbol] = FakePosition(
            symbol=symbol,
            side=side,
            size=Decimal(size),
            entry_price=self._mark(symbol, Decimal("100")),
            leverage=self.leverage.get(symbol, 3),
        )
