"""The end of a resting entry order's life (DECISION_LOGIC.md §8 rule 6; F-143, F-144).

An approved plan becomes a limit entry, and until this module nothing ever ended one. The design
said "unfilled entry older than `entry_valid_hours` → cancel", and the risk profile carried both
`entry_valid_hours` and `entry_drift_cancel_pct`, but no code read either: an unfilled entry
rested at OKX until someone noticed, and could fill days after the thesis it was planned for had
died. Separately, an order that left the venue — filled, or cancelled anywhere else — stayed
`OPEN` in the journal forever, because `reconcile` compares positions and never looked at orders.

One sweep, run by the review, settles both. For every entry the journal still thinks is resting:

* **Gone from the venue** → the journal catches up: `FILLED` if a position now exists on that
  asset, otherwise `CANCELLED`. Nothing is sent; there is nothing left to cancel.
* **Still resting, older than its validity** → cancelled at the venue *by id*, then `EXPIRED`.
* **Still resting, and the market ran away from it** → cancelled by id, then `CANCELLED`. "Away"
  is direction-aware: price falling towards a buy limit is the order working, not drift.

Cancelling is always by order id, never `cancel_all(symbol)`, which would also take a second,
younger entry resting on the same instrument. Protective stops are algo orders and never appear in
the regular book this reads, so no path here can touch one.

"Gone" is *asked*, not inferred: `fetch_order` returns the venue's status and filled quantity.
Inferring it from whether a position exists mislabels an entry that filled and then stopped out
between two reviews as cancelled — a real trade, with real PnL, vanishing from the accounting.
A row with no venue id — a legacy row, or one left NEW after a placement whose outcome was
unknown (X-N5) — is asked about by its own client order id, which every row carries. The position
heuristic survives only for a row the venue cannot be asked about at all.

Each row is settled on its own: one order that fails (filled between the read and the cancel, a
transient venue error) is reported and the sweep carries on, rather than leaving every later
stale entry live until the next review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import Engine, text

from argus.exchange.base import ExchangeAdapter
from argus.exchange.errors import redact, venue_error
from argus.exchange.reconcile import asset_of
from argus.execution.lifecycle import (
    apply_fill,
    filled_size,
    order_state,
    record_venue_ids,
    transition_order,
)

log = structlog.get_logger(__name__)

RESTING = ("OPEN", "PARTIAL")
# A NEW row is journalled *before* it is sent. Younger than this it may be a placement still in
# flight in a concurrent manager run, which holds a different lock than the review.
NEW_GRACE = timedelta(hours=1)


@dataclass(frozen=True)
class Swept:
    order_id: str
    symbol: str
    to_state: str
    reason: str


@dataclass
class SweepResult:
    settled: list[Swept] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _when(value: Any) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _venue_match(row: dict[str, Any], book: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The venue's copy of a journalled order: by venue id, else by our own client order id.

    Rows written before venue ids were stored (F-129, F-138) still carry `cl_ord_id`, which the
    venue echoes as `clientOrderId`. Matching on price instead would miss a row whose price was
    tick-rounded at send time, and could pair an order with a stranger at the same level.
    """
    venue_id = row.get("exchange_id")
    if venue_id:
        return next((o for o in book if str(o.get("id")) == str(venue_id)), None)
    cl = row.get("cl_ord_id")
    if cl:
        return next((o for o in book if str(o.get("clientOrderId") or "") == str(cl)), None)
    return None


def _drifted(row: dict[str, Any], last: Decimal, drift_pct: float) -> bool:
    if row.get("price") is None:
        return False
    entry = Decimal(str(row["price"]))
    band = entry * Decimal(str(drift_pct)) / Decimal(100)
    side = str(row["side"]).lower()
    if side in ("buy", "long"):
        return last > entry + band
    return last < entry - band


def _resting_entries(
    engine: Engine, now: datetime, *, venue: str
) -> list[tuple[dict[str, Any], str]]:
    """Entry orders still resting, or stuck NEW, that `venue`'s own runs placed (F-150).

    One journal holds both accounts (F-149). Another account's order is not on this venue, so
    "gone from the venue" would journal a demo entry CANCELLED while it still rests on demo,
    where nothing would sweep it again.
    """
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT o.order_id, o.cl_ord_id, o.run_id, o.symbol, o.side, o.price, o.size,"
                    " o.created_at, o.exchange_id FROM orders o"
                    " JOIN runs r ON r.run_id = o.run_id"
                    " WHERE o.kind = 'entry' AND r.config_hash = :venue"
                ),
                {"venue": venue},
            ).mappings()
        ]
    out: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        state = order_state(engine, row["order_id"])
        if state in RESTING:
            out.append((row, state))
        elif state == "NEW" and now - _when(row["created_at"]) > NEW_GRACE:
            out.append((row, state))
    return out


def _venue_fill(venue: dict[str, Any]) -> dict[str, Any] | None:
    """Price, size and fee of what the venue says filled — or None if any of them is unknown.

    The fee is OKX's own signed number (`info.fee`, negative for a charge, F-036); ccxt's
    positive `fee.cost` is the fallback, negated to the same convention. A fill with no reported
    fee is not written with a made-up zero (F-030).
    """
    size = Decimal(str(venue.get("filled") or 0))
    price = Decimal(str(venue.get("average") or 0))
    if size <= 0 or price <= 0:
        return None
    info = venue.get("info") or {}
    fee: Decimal | None = None
    ccy = info.get("feeCcy")
    if info.get("fee") not in (None, ""):
        fee = Decimal(str(info["fee"]))
    elif isinstance(venue.get("fee"), dict) and venue["fee"].get("cost") is not None:
        fee = -Decimal(str(venue["fee"]["cost"]))
        ccy = ccy or venue["fee"].get("currency")
    if fee is None or not ccy:
        return None
    return {
        "price": price,
        "size": size,
        "fee": fee,
        "fee_ccy": str(ccy),
        "trade_id": info.get("tradeId") or None,
    }


def _record_fill(
    engine: Engine, exchange: ExchangeAdapter, row: dict[str, Any], run_id: str | None
) -> None:
    """Write the venue's fill for this order as a `fills` row (review X-N11).

    `apply_fill` had no production caller: entries were marked FILLED with a bare transition, so
    realised PnL, fees and R rested on position snapshots. Only the part not yet recorded is
    written, so a second sweep never doubles a fill; `apply_fill` moves the order to PARTIAL or
    FILLED itself.
    """
    if row.get("exchange_id"):
        venue = exchange.fetch_order(str(row["symbol"]), str(row["exchange_id"]))
    else:
        found = exchange.fetch_order_by_client_id(str(row["symbol"]), str(row["cl_ord_id"]))
        if found is None:
            return
        venue = found
    fill = _venue_fill(venue)
    if fill is None:
        log.warning("resting.fill_unrecorded", order_id=row["order_id"], why="incomplete")
        return
    delta = fill["size"] - filled_size(engine, row["order_id"])
    if delta <= 0:
        return
    apply_fill(
        engine,
        run_id or str(row["run_id"]),
        row["order_id"],
        price=fill["price"],
        size=delta,
        fee=fill["fee"] * delta / fill["size"],
        fee_ccy=fill["fee_ccy"],
        exchange_fill_id=fill["trade_id"],
    )


def _settle(
    engine: Engine,
    exchange: ExchangeAdapter,
    row: dict[str, Any],
    state: str,
    target: str,
    reason: str,
    dry_run: bool,
    run_id: str | None = None,
) -> Swept:
    if not dry_run:
        current = state
        if current == "NEW" and target != "REJECTED":
            # A NEW row the venue knows about was accepted; the journal missed the OPEN step.
            transition_order(engine, row["order_id"], "OPEN", "venue holds it; OPEN was missed")
            current = "OPEN"
        if target == "FILLED" or "partially filled" in reason:
            try:
                _record_fill(engine, exchange, row, run_id)
            except Exception as exc:  # noqa: BLE001 - the state change must still land
                log.error("resting.fill_failed", order_id=row["order_id"], error=venue_error(exc))
            current = order_state(engine, row["order_id"])
        if current == "OPEN" and reason.startswith("partially filled"):
            transition_order(engine, row["order_id"], "PARTIAL", reason)
            current = "PARTIAL"
        if current != target:
            transition_order(engine, row["order_id"], target, reason)
    log.info("resting.settled", order_id=row["order_id"], to=target, reason=reason, dry=dry_run)
    return Swept(row["order_id"], str(row["symbol"]), target, reason)


def _venue_verdict(venue: dict[str, Any]) -> tuple[str, str] | None:
    status = str(venue.get("status") or "").lower()
    filled = Decimal(str(venue.get("filled") or 0))
    if status == "open":
        return None  # still live: it was missed by the book read, not gone
    if filled > 0:
        return "FILLED", f"venue status {status}, filled {filled}"
    return "CANCELLED", f"venue status {status}, nothing filled"


def _gone_verdict(
    exchange: ExchangeAdapter, row: dict[str, Any], state: str, held: set[str]
) -> tuple[str, str] | None:
    """What became of an order no longer in the book. None means: do not guess, ask a human.

    Asked by venue id, else by our client order id (review 2026-10-07): a row left NEW after an
    unknown placement outcome has no venue id, and guessing from positions journalled a filled
    and stopped-out entry REJECTED with no fills. A venue id learnt this way is written onto
    `row` for the caller to record.
    """
    venue_id = row.get("exchange_id")
    if venue_id:
        try:
            venue = exchange.fetch_order(str(row["symbol"]), str(venue_id))
        except LookupError as exc:
            # OKX purges an unfilled cancelled order after two hours and retains filled ones, so
            # "not in the book and not on record" means it died unfilled — unless a position on
            # the asset makes that ambiguous, in which case a human looks.
            if asset_of(row["symbol"]) in held:
                raise RuntimeError("venue purged it and a position is open; check by hand") from exc
            return "CANCELLED", "venue no longer holds it: purged after an unfilled cancel"
        return _venue_verdict(venue)
    cl_ord_id = row.get("cl_ord_id")
    if cl_ord_id:
        found = exchange.fetch_order_by_client_id(str(row["symbol"]), str(cl_ord_id))
        if found is not None:
            if found.get("id"):
                row["exchange_id"] = str(found["id"])
            return _venue_verdict(found)
        # Filled orders are retained, so no record of our client id means nothing filled.
        if state == "NEW":
            return "REJECTED", "the venue holds no order with this client id: never reached it"
        if asset_of(row["symbol"]) in held:
            raise RuntimeError("venue holds no order with this client id and a position is open")
        return "CANCELLED", "venue no longer holds it: purged after an unfilled cancel"
    # Nothing to ask the venue about.
    if asset_of(row["symbol"]) in held:
        return None  # an open position on this asset could be this order or another; not guessing
    if state == "NEW":
        return "REJECTED", "journalled but never reached the venue"
    return "CANCELLED", "gone from the venue with no position (legacy row, no venue id)"


def sweep(
    engine: Engine,
    exchange: ExchangeAdapter,
    now: datetime,
    valid_hours: float,
    drift_pct: float,
    dry_run: bool = False,
    run_id: str | None = None,
    *,
    venue: str,
) -> SweepResult:
    """Close every resting entry whose life is over. Reports what changed (or would have).

    `run_id` attributes recorded fills to the sweeping run; without it a fill is attributed to
    the run that placed the order. `venue` is the account `exchange` trades: only entries its own
    runs placed are swept (F-150).
    """
    result = SweepResult()
    pending = _resting_entries(engine, now, venue=venue)
    if not pending:
        return result
    book = exchange.fetch_open_orders()
    held = {asset_of(p.get("symbol", "")) for p in exchange.fetch_positions()}

    for row, state in pending:
        try:
            known_id = row.get("exchange_id")
            outcome = _sweep_one(exchange, row, state, book, held, now, valid_hours, drift_pct)
            if row.get("exchange_id") and not known_id and not dry_run:
                # Learnt by client id: on the row, so the next read needs no lookup (F-129).
                record_venue_ids(engine, row["order_id"], {"entry_id": str(row["exchange_id"])})
            if outcome is None:
                continue
            target, reason, venue_id = outcome
            if venue_id is not None and not dry_run:
                try:
                    exchange.cancel_order(row["symbol"], venue_id)
                except Exception as exc:  # noqa: BLE001 - it may have filled since the read
                    gone = _gone_verdict(exchange, row | {"exchange_id": venue_id}, state, held)
                    if gone is None:
                        raise RuntimeError(f"cancel failed and order still live: {exc}") from exc
                    target, reason = gone
            result.settled.append(
                _settle(engine, exchange, row, state, target, reason, dry_run, run_id)
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not strand the rest
            # Redacted (review S-3): these reach the review's report and chat verbatim.
            result.errors.append(f"{row['symbol']} order {row['order_id']}: {redact(str(exc))}")
            log.error("resting.failed", order_id=row["order_id"], error=venue_error(exc))
    return result


def _sweep_one(
    exchange: ExchangeAdapter,
    row: dict[str, Any],
    state: str,
    book: list[dict[str, Any]],
    held: set[str],
    now: datetime,
    valid_hours: float,
    drift_pct: float,
) -> tuple[str, str, str | None] | None:
    """(target state, reason, venue id to cancel or None), or None when nothing should change."""
    venue = _venue_match(row, book)
    if venue is None:
        gone = _gone_verdict(exchange, row, state, held)
        if gone is None and not row.get("exchange_id") and asset_of(row["symbol"]) in held:
            raise RuntimeError("legacy row gone from the book with a position open; check by hand")
        return None if gone is None else (*gone, None)

    filled = Decimal(str(venue.get("filled") or 0))
    prefix = f"partially filled {filled}; " if filled > 0 else ""
    age = now - _when(row["created_at"])
    if age > timedelta(hours=valid_hours):
        hours = age.total_seconds() / 3600
        return (
            "EXPIRED",
            f"{prefix}unfilled for {hours:.1f}h (valid {valid_hours:g}h)",
            str(venue["id"]),
        )
    last = Decimal(str(exchange.fetch_ticker(row["symbol"])["last"]))
    if not _drifted(row, last, drift_pct):
        return None
    reason = f"{prefix}drift: last {last} is more than {drift_pct:g}% beyond entry {row['price']}"
    return "CANCELLED", reason, str(venue["id"])
