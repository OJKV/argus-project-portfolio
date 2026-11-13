"""Recording an order's life, and refusing to record an impossible one.

Every state change goes through `journal/states.py`, so a retry bug that would jump an order
from NEW straight to FILLED raises here instead of writing a row nobody can explain a month
later. `orders` and `fills` are hash-chained ledger tables: rows are appended, never updated, so
the history survives even when the latest state turns out to be wrong.

Money is stored as TEXT. A float price is a rounding error waiting for a reconciliation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import Engine, text

from argus.journal import ledger
from argus.journal.db import new_id
from argus.journal.states import OrderState, can_transition

log = structlog.get_logger(__name__)


class IllegalTransitionError(RuntimeError):
    """The state machine refused. The row is not written."""


def record_order(
    engine: Engine, run_id: str, order: dict[str, Any], plan_id: str | None = None
) -> str:
    """Append a NEW order. The unique `cl_ord_id` is what makes a retry a no-op at the database."""
    order_id = new_id()
    ledger.append(
        engine,
        "orders",
        {
            "order_id": order_id,
            "cl_ord_id": order["cl_ord_id"],
            "run_id": run_id,
            "plan_id": plan_id,
            "symbol": order["symbol"],
            "side": order["side"],
            "kind": order.get("kind", "entry"),
            "order_type": order["order_type"],
            "price": None if order.get("price") is None else str(order["price"]),
            "size": str(order["size"]),
            "state": OrderState.NEW.value,
            "raw_request": json.dumps(order, default=str),
        },
    )
    return order_id


def record_venue_ids(engine: Engine, order_id: str, ids: dict[str, str]) -> None:
    """Store what the venue called this order, on the order row.

    `orders` is write-once, so this is the one exception the schema allows: the columns exist
    precisely to be filled in once the venue answers, and they start NULL because we do not know
    the ids until then. Keeping them only in a free-text `position_events.reason` meant nothing
    could join an order to the venue programmatically — `reconcile` compares by symbol and side,
    and could not tell two orders on the same symbol apart (F-129).

    These three columns are outside the row hash (`ledger.WRITE_ONCE`), so filling them keeps the
    `orders` chain verifiable (J-N1), and the `orders_write_once` trigger refuses to change one
    that is already set. COALESCE keeps a repeated call from tripping it: the order is live at
    the venue by now, and an exception here would skip journalling it as OPEN.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE orders SET exchange_id = COALESCE(exchange_id, :x),"
                " algo_id = COALESCE(algo_id, :a), raw_response = COALESCE(raw_response, :raw)"
                " WHERE order_id = :o"
            ),
            {
                # `entry_id` is the adapters' contract — see `ExchangeAdapter.place_bracket`.
                "x": ids.get("entry_id") or ids.get("entry"),
                "a": ids.get("sl_id") or ids.get("sl") or ids.get("algo"),
                "raw": json.dumps(ids, default=str),
                "o": order_id,
            },
        )


def _order_row(engine: Engine, order_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = (
            conn.execute(text("SELECT * FROM orders WHERE order_id = :o"), {"o": order_id})
            .mappings()
            .first()
        )
    if row is None:
        raise LookupError(f"no order {order_id}")
    return dict(row)


def order_state(engine: Engine, order_id: str) -> str:
    """The latest state from `position_events`, or NEW if it has never moved.

    `orders` holds the immutable facts of an order and `position_events` holds its life. Both
    are hash-chained and insert-only, which is why the state is derived rather than updated —
    an UPDATE would break the chain and erase the history that makes a fill explainable.
    """
    with engine.connect() as conn:
        state = conn.execute(
            text(
                "SELECT to_state FROM position_events"
                " WHERE machine = 'order' AND json_extract(detail, '$.order_id') = :o"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ),
            {"o": order_id},
        ).scalar()
    return str(state) if state else OrderState.NEW.value


def transition_order(engine: Engine, order_id: str, new_state: str, reason: str = "") -> str:
    """Move an order, or refuse. Refusing writes nothing."""
    row = _order_row(engine, order_id)
    current = order_state(engine, order_id)
    if not can_transition(OrderState, current, new_state):
        raise IllegalTransitionError(
            f"order {order_id}: {current} -> {new_state} is not a legal transition"
        )
    ledger.append(
        engine,
        "position_events",
        {
            "run_id": row["run_id"],
            "symbol": row["symbol"],
            "plan_id": row["plan_id"],
            "machine": "order",
            "from_state": current,
            "to_state": new_state,
            "reason": reason or f"-> {new_state}",
            "detail": json.dumps({"order_id": order_id}),
        },
    )
    log.info("order.transition", order_id=order_id, frm=current, to=new_state, reason=reason)
    return new_state


def filled_size(engine: Engine, order_id: str) -> Decimal:
    with engine.connect() as conn:
        rows = (
            conn.execute(text("SELECT size FROM fills WHERE order_id = :o"), {"o": order_id})
            .scalars()
            .all()
        )
    return sum((Decimal(str(s)) for s in rows), Decimal("0"))


def apply_fill(
    engine: Engine,
    run_id: str,
    order_id: str,
    price: Decimal,
    size: Decimal,
    fee: Decimal,
    fee_ccy: str,
    exchange_fill_id: str | None = None,
) -> str:
    """Record a fill and move the order to PARTIAL or FILLED.

    An overfill raises: more filled than ordered means we are reading someone else's fill, or
    reading ours twice, and both are worse than a loud stop.
    """
    row = _order_row(engine, order_id)
    ordered = Decimal(str(row["size"]))
    already = filled_size(engine, order_id)
    if already + size > ordered:
        raise ValueError(f"fill of {size} exceeds the {ordered} ordered ({already} already filled)")

    fill_id = new_id()
    ledger.append(
        engine,
        "fills",
        {
            "fill_id": fill_id,
            "order_id": order_id,
            "run_id": run_id,
            "exchange_fill_id": exchange_fill_id,
            "symbol": row["symbol"],
            "side": row["side"],
            "price": str(price),
            "size": str(size),
            "fee": str(fee),
            "fee_ccy": fee_ccy,
            "raw": json.dumps({"price": str(price), "size": str(size)}),
        },
    )
    total = already + size
    target = OrderState.FILLED.value if total >= ordered else OrderState.PARTIAL.value
    state = order_state(engine, order_id)
    if state == OrderState.NEW.value:
        # A fill proves the venue accepted the order; the journal missed the OPEN step.
        transition_order(engine, order_id, OrderState.OPEN.value, "fill seen before OPEN")
        state = OrderState.OPEN.value
    if state != target:
        if can_transition(OrderState, state, target):
            transition_order(engine, order_id, target, reason=f"filled {total}/{ordered}")
        else:
            log.error(
                "fill.transition_skipped",
                order_id=order_id,
                frm=state,
                to=target,
                fill_id=fill_id,
            )
    return fill_id
