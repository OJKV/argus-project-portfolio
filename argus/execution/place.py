"""Placing an approved plan, and refusing to leave a position naked while doing it.

This is the only path from a gate verdict to a live order. Everything it does is arranged around
one rule (CLAUDE.md #4): **every position has an exchange-side stop**. The stop is not a follow-up
call — it travels with the entry in `attach_algos`, so the venue itself refuses an entry it cannot
protect, and there is no window between "filled" and "protected" for a crash to land in.

The order of operations matters and is deliberate:

1. `build_entry_order` raises rather than construct an unprotected order — a stop on the wrong
   side of the entry is a type error here, not a rejection later.
2. The order is **journalled before it is sent**. If the process dies between the two, the row
   exists and reconcile can find the orphan. The reverse order would place money with no record.
3. Only then does it reach the venue.
4. Whatever happens next is written down: submitted, or rejected with the reason.

`cl_ord_id` is derived from (run, instrument, side), so within one run the same intention always
carries the same id: `place_bracket` asks the venue for it before sending (review X-8) and the
unique index rejects a duplicate row. A *new* run is a new intention with a new id — what stops it
doubling a live position is the gate seeing the real book, not the id.

When the venue call raises, the venue is asked for the id before anything is written (review
X-N5). A timeout whose reply was lost is an order that may well be live; journalling it REJECTED
left a real position that nothing would amend, cancel or reconcile. Found → OPEN with its venue id.
Not found → REJECTED. Could not ask after a network failure → left NEW ("sent, outcome unknown"),
which the resting sweep and the watchdog's orphan check settle later.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import Engine

from argus.exchange.errors import outcome_unknown, venue_error
from argus.execution.lifecycle import record_order, record_venue_ids, transition_order
from argus.execution.orders import UnprotectedOrderError, build_entry_order
from argus.gate.rules import GateResult, PlanInput
from argus.journal.states import OrderState

log = structlog.get_logger(__name__)

# Both are approvals. `ADJUSTED` means the gate changed the size or capped the leverage and is
# content for the trade to happen on those terms; refusing to place it would silently discard a
# decision the gate already made.
PLACEABLE = ("APPROVED", "ADJUSTED")


class PlacementError(RuntimeError):
    """The venue refused, or answered with something we will not act on."""


def place_approved(
    engine: Engine,
    adapter: Any,
    run_id: str,
    plan: PlanInput,
    gate: GateResult,
    plan_id: str,
    instrument: str,
) -> str | None:
    """Place one approved plan. Returns the journal's order id, or None if nothing was placed."""
    if gate.status not in PLACEABLE:
        return None
    if gate.sized is None or not gate.sized.ok:
        # Reaching here would mean the gate approved a plan it could not size, which is a bug
        # rather than a market condition — say so instead of guessing a size.
        raise PlacementError(f"{plan.symbol}: gate said {gate.status} with no usable size")

    order = build_entry_order(plan, gate.sized, run_id, instrument)

    # Journal first. A row with no order is an orphan reconcile can see; an order with no row is
    # money the system does not know it spent.
    order_id = record_order(engine, run_id, order, plan_id=plan_id)

    try:
        adapter.set_leverage(instrument, gate.sized.leverage)
        ids = adapter.place_bracket(order)
    except Exception as exc:
        # Redacted (review S-3): ccxt's message is OKX's whole response body, which on a 50110
        # names the VPS's address — and this reason is a hash-chained row nobody can edit later.
        reason = venue_error(exc)
        held, unknown = _ask_venue(adapter, instrument, str(order["cl_ord_id"]))
        if held is not None and held.get("id"):
            # The venue has it: the error was about the reply, not the order (review X-N5).
            ids = {"entry_id": str(held["id"]), "recovered_after": reason}
            record_venue_ids(engine, order_id, ids)
            transition_order(
                engine, order_id, OrderState.OPEN.value, reason=f"venue holds it after {reason}"
            )
            log.warning("execution.placed_despite_error", symbol=plan.symbol, error=reason)
            return order_id
        if unknown and outcome_unknown(exc):
            log.error("execution.place_unknown", symbol=plan.symbol, error=reason)
            raise PlacementError(
                f"{plan.symbol}: outcome unknown after {reason}; left NEW for the resting sweep"
            ) from exc
        transition_order(engine, order_id, OrderState.REJECTED.value, reason=reason)
        log.error("execution.place_failed", symbol=plan.symbol, error=reason)
        raise PlacementError(f"{plan.symbol}: {reason}") from exc

    # `entry_id` is the key BOTH adapters return; this asked for `entry` and so treated every
    # successful placement as a failure — journalling REJECTED for an order that was live at the
    # venue, which is worse than the placement simply failing (F-138).
    if not ids or not ids.get("entry_id"):
        # No id means we cannot amend, cancel or reconcile this order later. Treat it as a
        # failure loudly rather than carry an untrackable position.
        transition_order(engine, order_id, OrderState.REJECTED.value, reason="venue returned no id")
        raise PlacementError(f"{plan.symbol}: venue returned no order id")

    # On the row, not only in a reason string: without this nothing can join an order to the
    # venue, and an amend or cancel later has no id to use (F-129).
    record_venue_ids(engine, order_id, ids)
    transition_order(
        engine, order_id, OrderState.OPEN.value, reason=f"venue ids {sorted(ids.items())}"
    )
    log.info(
        "execution.placed",
        symbol=plan.symbol,
        side=plan.side,
        contracts=str(gate.sized.contracts),
        leverage=gate.sized.leverage,
        ids=ids,
    )
    return order_id


def _ask_venue(adapter: Any, instrument: str, cl_ord_id: str) -> tuple[dict[str, Any] | None, bool]:
    """(the venue's copy of the order or None, whether the answer is unknown).

    An adapter without the lookup, or a lookup that fails, is "unknown" — distinct from the
    venue saying it holds no such order.
    """
    lookup = getattr(adapter, "fetch_order_by_client_id", None)
    if lookup is None:
        return None, True
    try:
        held = lookup(instrument, cl_ord_id)
    except Exception as exc:  # noqa: BLE001 - "could not ask" is reported as unknown
        log.error("execution.lookup_failed", instrument=instrument, error=venue_error(exc))
        return None, True
    return (dict(held) if held else None), False


__all__ = ["PLACEABLE", "PlacementError", "UnprotectedOrderError", "place_approved"]
