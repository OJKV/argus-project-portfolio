"""Turning an approved plan into an exchange order (ARCHITECTURE.md §8, DECISION_LOGIC.md §7).

One idea shapes this module: **an order without a stop must be impossible to construct**, not
merely rejected somewhere downstream. AGENTS.md rule 4 says every position has an exchange-side
stop, and the only way to guarantee that is to make the unprotected case a type error rather than
a thing to remember.

The size never comes from the plan. The model proposes levels; `argus/gate/sizing.py` decides how
much, and this function copies that number without opinion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from argus.execution.idempotency import client_order_id
from argus.gate.rules import PlanInput
from argus.gate.sizing import SizingResult


class UnprotectedOrderError(ValueError):
    """Raised rather than build an order that could leave a position naked."""


def _decimal(value: float, field: str) -> Decimal:
    if value is None or value <= 0:
        raise UnprotectedOrderError(f"{field} must be a positive price, got {value!r}")
    return Decimal(str(value))


def build_entry_order(
    plan: PlanInput,
    sized: SizingResult,
    run_id: str,
    instrument: str,
) -> dict[str, Any]:
    """The payload for `ExchangeAdapter.place_bracket`. Pure: nothing here talks to a venue."""
    if not sized.ok or sized.contracts <= 0:
        raise UnprotectedOrderError(
            f"sizing did not approve this plan: {sized.reason or 'no size'}"
        )

    stop = _decimal(plan.stop, "stop")
    entry_low = _decimal(plan.entry_low, "entry_low")
    entry_high = _decimal(plan.entry_high, "entry_high")
    long = plan.side == "long"

    # The gate checks this too. Checking again here is deliberate: this function is the last
    # place before the exchange, and a stop on the wrong side is an order OKX rejects — leaving
    # a filled entry with no protection, which is the exact state rule 4 exists to prevent.
    if long and stop >= entry_low:
        raise UnprotectedOrderError(f"long stop {stop} is not below the entry band {entry_low}")
    if not long and stop <= entry_high:
        raise UnprotectedOrderError(f"short stop {stop} is not above the entry band {entry_high}")

    attach: list[dict[str, Any]] = [{"kind": "sl", "trigger_px": stop}]
    if plan.targets:
        # One TP attaches to an entry on OKX. Later targets are the Reviewer's to manage, and
        # attaching only the first keeps the exchange's state and our intention in agreement.
        attach.append({"kind": "tp", "trigger_px": _decimal(plan.targets[0].price, "target")})

    order: dict[str, Any] = {
        "symbol": instrument,
        "side": "buy" if long else "sell",
        "size": sized.contracts,
        "leverage": sized.leverage,
        "order_type": plan.entry_kind,
        "cl_ord_id": client_order_id(run_id, instrument, plan.side),
        "attach_algos": attach,
        "reduce_only": False,
    }
    if plan.entry_kind == "limit":
        # Rest at the edge of the band that favours us: buying low, selling high. A limit in the
        # middle is a worse fill for no benefit — the band is where we are willing to be filled,
        # not a prediction.
        order["price"] = entry_low if long else entry_high
    return order
