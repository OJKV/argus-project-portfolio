"""WATCHDOG — the no-LLM safety run (ARCHITECTURE.md §3, §14).

Every 20 minutes, in this order:
  1. HALT file honoured (checked first: a halted system still reports, never trades);
  2. reconcile — the exchange is truth (F-006);
  3. every position must have a live exchange-side SL or trailing algo; repair if not (F-003);
  4. margin ratio floor;
  5. portfolio drawdown kill-switch vs the 30d equity high → close all + HALT;
  6. healthcheck ping.

It holds no opinions about the market. It costs zero LLM calls, and the only orders it ever
places are protective ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import Engine, text

from argus.config import Settings, get_settings
from argus.exchange.base import ExchangeAdapter
from argus.exchange.breakers import BreakerRegistry
from argus.exchange.errors import venue_error
from argus.exchange.reconcile import asset_of
from argus.exchange.reconcile import reconcile as journal_reconcile
from argus.gate import killswitch
from argus.journal import ledger
from argus.journal import positions as positions_journal
from argus.journal.db import new_id, utc_now
from argus.notify.telegram import Notifier
from argus.observability import alerts, metrics, spans

log = structlog.get_logger(__name__)

# Portfolio kill-switch and margin floor (DECISION_LOGIC.md §9) — the Risk Gate's numbers, read
# from `gate/killswitch.py`, never restated here (review X-19: two copies could drift apart).
# As Decimal for the watchdog's arithmetic; `:g` keeps 15.0 printing as "15", as before.
DD_HALT_PCT = Decimal(f"{killswitch.PORTFOLIO_DD_LIMIT_PCT:g}")
MARGIN_RATIO_FLOOR = Decimal(f"{killswitch.MARGIN_RATIO_FLOOR_PCT:g}")
UNREACHABLE_ALERT_AFTER = 3
BREAKER_DEPENDENCY = "exchange"
# An orphan is paged when first seen, then at most this often while it stays (review X-N2).
ORPHAN_REALERT = timedelta(hours=24)
DEFAULT_STOP_ATR_MULT = Decimal("2")
# An emergency stop may sit at most this fraction of the way to the liquidation price.
LIQUIDATION_CLAMP = Decimal("0.5")
# Marks a HALT written by the kill-switch rather than by an operator's `touch HALT`. A halted
# tick retries an unfinished flatten only for the former (review X-4).
KILL_MARKER = "kill-switch:"
# Marks a HALT written for a drawdown breach the second balance read could not confirm (review
# 2026-10-07). It stops new entries, never arms the flatten retry, and keeps the drawdown check
# running on halted ticks so a breach confirmed later still flattens. The operator clears it.
DD_UNCONFIRMED_MARKER = "drawdown-unconfirmed:"


@dataclass
class WatchdogResult:
    run_id: str | None = None
    halted: bool = False
    positions: int = 0
    repaired: list[str] = field(default_factory=list)
    closed_past_stop: list[str] = field(default_factory=list)
    repair_failures: list[str] = field(default_factory=list)
    close_failures: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    margin_warnings: list[str] = field(default_factory=list)
    drawdown_pct: Decimal | None = None
    killed: bool = False
    unreachable: bool = False
    # Consecutive watchdog ticks, this one included, that could not reach the venue (X-10).
    unreachable_streak: int = 0
    notes: list[str] = field(default_factory=list)
    # Journal versus venue (exchange/reconcile.py, review X-N2): "kind symbol: detail".
    discrepancies: list[str] = field(default_factory=list)
    # A step that could not run (balance unreadable, equity history unreadable, an unexpected
    # exception). Not a venue hiccup and not a protection failure, but the pass did not do all of
    # its job, so it must not report clean (review X-N6).
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.repair_failures
            or self.close_failures
            or self.killed
            or self.unreachable
            or self.errors
        )

    def degraded_not_failed(self, engine: Any, *, venue: str) -> bool:
        """Was this a venue hiccup with nothing at stake, rather than an incident?

        OKX puts single algo-order types into maintenance, and `collect_algo_orders` refuses a
        partial answer — correctly, because a partial algo list can make a protected position look
        naked. The cost was that every maintenance window exited non-zero: three failed units in
        three days with an empty book (F-140). An operator who sees `Failed` for routine venue
        weather learns to ignore the one that matters.

        The question that decides it is whether money was on the table, and the venue cannot be
        asked while it is unreachable — so the journal is asked instead. `positions_snapshots`
        holds the last book we actually saw. An empty one means an unreachable venue put nothing
        at risk; anything open means we could not verify its stop, which is an incident. The
        book is `venue`'s own: another account's rows neither raise nor hide one (F-149).

        Never downgrades a repair or close failure: reaching the venue and failing to protect
        something is the worst case regardless of what else happened.
        """
        if self.repair_failures or self.close_failures or self.killed or self.errors:
            return False
        if not self.unreachable:
            return False
        try:
            from argus.journal import positions as positions_journal

            return not positions_journal.latest_open(engine, venue=venue)
        except Exception:  # noqa: BLE001 - if we cannot even ask, assume the worse case
            return False


def read_halt(path: Path) -> str | None:
    """Existence is the halt; the text is only the reason (review 2026-09-18, X-5).

    `touch HALT` is the only halt method an operator has today. The watchdog used to read the file
    and test the *text* for truthiness, so an empty file halted the manager (`exists()`) and not
    the watchdog — which went on to run the drawdown branch that can close every position.
    """
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or "halt (no reason given)"


def write_halt(path: Path, reason: str) -> None:
    """Atomic: a half-written HALT file must never read as 'no halt'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{utc_now()} {reason}\n", encoding="utf-8")
    tmp.replace(path)


def clear_halt(path: Path) -> None:
    path.unlink(missing_ok=True)


PEAK_WINDOW = timedelta(days=30)


def equity_peak_30d(
    engine: Engine,
    current: Decimal,
    *,
    venue: str,
    now: datetime | None = None,
) -> Decimal:
    """Highest equity `venue` recorded in the last 30 days, never below the current equity.

    The cutoff is computed here and bound in the journal's own ISO format (review X-18). It was
    SQLite's `datetime('now', '-30 days')`, which renders with a space where the journal writes
    `T`; `T` sorts after a space, so every snapshot from the boundary *day* stayed in the window.

    Only snapshots from runs tagged with `venue` count (review X-3). Runs carry the venue in
    `config_hash` (F-131); without the filter a journal used on demo ($10,000) and then live
    ($200) read the live account as a 98% drawdown, wrote HALT and flattened it. The account is
    required, not optional (F-149): the optional filter was left off by the dashboard, which
    then showed a kill-switch figure the watchdog never acts on.
    """
    cutoff = ((now or datetime.now(UTC)) - PEAK_WINDOW).replace(microsecond=0).isoformat()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT MAX(CAST(e.equity AS REAL)) FROM equity_snapshots e"
                " JOIN runs r ON r.run_id = e.run_id"
                " WHERE e.created_at >= :c AND r.config_hash = :venue"
            ),
            {"c": cutoff, "venue": venue},
        ).scalar()
    peak = Decimal(str(row)) if row else Decimal("0")
    return max(peak, current)


def record_equity(engine: Engine, run_id: str, equity: Decimal) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO equity_snapshots (snapshot_id, run_id, created_at, equity) "
                "VALUES (:i, :r, :t, :e)"
            ),
            {"i": new_id(), "r": run_id, "t": utc_now(), "e": str(equity)},
        )


def unreachable_streak(engine: Engine, run_id: str, reachable: bool, detail: str = "") -> int:
    """Journal the venue's reachability and return how many ticks in a row it has been down.

    Review X-10: every tick is a fresh process with a fresh in-memory `BreakerRegistry`, so the
    old `attempts >= 3 and state == "open"` could never be true in production and the
    `exchange_unreachable` alert never fired. The count lives in `circuit_breaker_events`, where
    the next process can read it: one `open` row per failed tick, one `closed` row when the venue
    answers again. Only transitions are written on success, so a healthy venue adds nothing.
    """
    with engine.begin() as conn:
        last_ok = conn.execute(
            text(
                "SELECT COALESCE(MAX(rowid), 0) FROM circuit_breaker_events"
                " WHERE dependency = :d AND to_state = 'closed'"
            ),
            {"d": BREAKER_DEPENDENCY},
        ).scalar()
        failed = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM circuit_breaker_events"
                    " WHERE dependency = :d AND to_state = 'open' AND rowid > :r"
                ),
                {"d": BREAKER_DEPENDENCY, "r": last_ok},
            ).scalar()
            or 0
        )
        if reachable and not failed:
            return 0
        streak = 0 if reachable else failed + 1
        conn.execute(
            text(
                "INSERT INTO circuit_breaker_events (event_id, run_id, created_at, dependency,"
                " from_state, to_state, attempts, detail) VALUES (:i, :r, :t, :d, :f, :s, :a, :x)"
            ),
            {
                "i": new_id(),
                "r": run_id,
                "t": utc_now(),
                "d": BREAKER_DEPENDENCY,
                "f": "open" if failed else "closed",
                "s": "closed" if reachable else "open",
                "a": streak,
                "x": detail[:500],
            },
        )
    return streak


def _stop_already_breached(side: str, stop: Decimal, mark: Decimal) -> bool:
    """True when the market has passed the level the stop would sit at."""
    return mark <= stop if side == "long" else mark >= stop


# An entry the venue accepted: its position may be live. A NEW row was never confirmed sent, and
# a REJECTED one never reached the book.
_PLACED_STATES = frozenset({"OPEN", "PARTIAL", "FILLED"})
_FILLED_STATES = frozenset({"PARTIAL", "FILLED"})

PlanStops = dict[tuple[str, str], Decimal]


def _position_side(side: str) -> str:
    """`buy`/`long` -> long, `sell`/`short` -> short. Orders and positions use different words."""
    return "long" if str(side).strip().lower() in ("long", "buy") else "short"


def _sent_stop(raw_request: str | None, payload: str | None) -> Decimal | None:
    """The stop that travelled with the entry, else the plan's own number."""
    for blob, path in ((raw_request, "attach_algos"), (payload, None)):
        try:
            data = json.loads(blob or "")
        except (TypeError, ValueError):
            continue
        if path is None:
            value = data.get("stop") if isinstance(data, dict) else None
        else:
            algos = data.get(path) if isinstance(data, dict) else None
            value = next((a.get("trigger_px") for a in algos or [] if a.get("kind") == "sl"), None)
        if value not in (None, ""):
            return Decimal(str(value))
    return None


def plan_stops_from_journal(
    engine: Engine, open_orders: list[dict[str, Any]], *, venue: str
) -> PlanStops:
    """The stop the Risk Gate approved for each position the venue holds, keyed by (asset, side).

    Review X-6 wired the journal into the production watchdog; review X-N3 found what the first
    version read: every non-rejected plan, keyed by asset alone, latest row winning. A long BTC
    from plan A (stop 95k) and a later, never-placed **short** plan B (stop 110k) left BTC's repair
    stop at 110k — above the long's mark, so the watchdog's breach check market-closed a healthy
    position. So the stop is the one that travelled with an accepted entry, and the key carries
    the side. `asset_of` normalises `BTC/USDT:USDT` and `BTC-USDT-SWAP` to one key.

    Review 2026-10-07 narrowed "accepted" to the entries that make up the position *now* —
    `current_entries`, the same set `journal_positions` reconciles. A same-side limit entry still
    resting in the book is not the position, and its (tighter) stop used to replace the filled
    one's; an entry of a trade that has since closed is not either, and an orphan used to inherit
    its stop. An orphan therefore has no plan stop and is repaired from the ATR.

    Several entries can make up one position in net mode. One the journal has seen fill is
    preferred to one only presumed filled (accepted and gone from the book — it may have been
    cancelled elsewhere and not yet swept); within that, the newest stop is the live thesis.

    Only `venue`'s entries count (F-149): a later demo entry on the same asset used to supply the
    repair stop for a live position, and could widen it.
    """
    best: dict[tuple[str, str], tuple[bool, Decimal]] = {}
    for entry in current_entries(engine, open_orders, venue=venue):
        stop = _sent_stop(entry["raw_request"], entry["payload"])
        if stop is None or stop <= 0:
            continue
        key = (asset_of(entry["symbol"]), entry["side"])
        held = best.get(key)
        if held is None or entry["filled"] or not held[0]:
            best[key] = (bool(entry["filled"]), stop)
    return {key: stop for key, (_, stop) in best.items()}


def _placed_entries(engine: Engine, venue: str) -> list[dict[str, Any]]:
    """Entry orders the venue accepted, oldest first, each with its latest journalled state.

    "Accepted" is latest state OPEN/PARTIAL/FILLED, or an order that ever filled (a PARTIAL that
    was later cancelled still left a position behind). The state is derived from
    `position_events` exactly as `lifecycle.order_state` does, in one query instead of one per
    order. Only orders placed by `venue`'s runs (F-149).
    """
    with engine.connect() as conn:
        orders = conn.execute(
            text(
                "SELECT o.order_id, o.symbol, o.side, o.size, o.created_at, o.exchange_id,"
                " o.cl_ord_id, o.plan_id, o.raw_request, p.payload FROM orders o"
                " LEFT JOIN plans p ON p.plan_id = o.plan_id"
                " JOIN runs r ON r.run_id = o.run_id"
                " WHERE o.kind = 'entry' AND r.config_hash = :venue"
                " ORDER BY o.created_at, o.rowid"
            ),
            {"venue": venue},
        ).all()
        seen: dict[str, set[str]] = {}
        latest: dict[str, str] = {}
        for order_id, to_state in conn.execute(
            text(
                "SELECT json_extract(detail, '$.order_id'), to_state FROM position_events"
                " WHERE machine = 'order' ORDER BY created_at, rowid"
            )
        ).all():
            seen.setdefault(str(order_id), set()).add(str(to_state))
            latest[str(order_id)] = str(to_state)
    out: list[dict[str, Any]] = []
    for row in orders:
        order_id = str(row.order_id)
        state = latest.get(order_id, "NEW")
        filled = bool(seen.get(order_id, set()) & _FILLED_STATES)
        if state not in _PLACED_STATES and not filled:
            continue
        out.append(
            {
                "order_id": order_id,
                "symbol": str(row.symbol),
                "side": _position_side(str(row.side)),
                "size": Decimal(str(row.size or 0)),
                "created_at": str(row.created_at),
                "exchange_id": row.exchange_id,
                "cl_ord_id": row.cl_ord_id,
                "plan_id": row.plan_id,
                "state": state,
                "filled": filled,
                "raw_request": row.raw_request,
                "payload": row.payload,
            }
        )
    return out


def _order_keys(order: dict[str, Any]) -> set[str]:
    """The venue id and our client id of a venue order — either one identifies the entry."""
    return {str(order[k]) for k in ("id", "clientOrderId") if order.get(k)}


def _entry_keys(entry: dict[str, Any]) -> set[str]:
    return {str(entry[k]) for k in ("exchange_id", "cl_ord_id") if entry.get(k)}


def resting_through(symbols: list[str], open_orders: list[dict[str, Any]]) -> dict[str, list[str]]:
    """For each closing symbol, the keys of the orders resting on its asset (review 2026-10-07).

    Written onto the CLOSED snapshot: an entry still resting when a close is noticed did not
    belong to the closed trade, whenever it was created.
    """
    out: dict[str, list[str]] = {}
    for symbol in symbols:
        asset = asset_of(symbol)
        keys: set[str] = set()
        for order in open_orders:
            if asset_of(str(order.get("symbol") or "")) == asset:
                keys |= _order_keys(order)
        out[symbol] = sorted(keys)
    return out


def _last_closes(engine: Engine, venue: str) -> dict[str, tuple[str, frozenset[str]]]:
    """Per asset: `venue`'s latest CLOSED snapshot's stamp, and what was resting at that close."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.symbol, c.created_at, c.raw FROM positions_snapshots c"
                " JOIN runs r ON r.run_id = c.run_id"
                " WHERE c.state = 'CLOSED' AND r.config_hash = :venue"
            ),
            {"venue": venue},
        ).all()
    closes: dict[str, tuple[str, frozenset[str]]] = {}
    for symbol, stamp, raw in rows:
        asset = asset_of(str(symbol))
        if str(stamp) <= closes.get(asset, ("", frozenset()))[0]:
            continue
        try:
            resting = json.loads(raw or "{}").get("resting") or []
        except (TypeError, ValueError, AttributeError):
            resting = []
        closes[asset] = (str(stamp), frozenset(str(k) for k in resting))
    return closes


def current_entries(
    engine: Engine, open_orders: list[dict[str, Any]], *, venue: str
) -> list[dict[str, Any]]:
    """Accepted entries that make up what the venue holds now, oldest first.

    An entry counts unless it is still resting in the book (a resting entry is not a position
    yet; a partially filled one is) or it belonged to a trade that has since closed: created
    before the asset's last CLOSED snapshot and not resting when that close was noticed. The
    snapshot is stamped when the close is *noticed*, up to a watchdog interval after the stop
    filled, so an entry placed in that gap rested through it and is the next position — dropping
    it by creation time alone paged its fill as an orphan (review 2026-10-07).
    """
    resting = set().union(*(_order_keys(o) for o in open_orders)) if open_orders else set()
    closes = _last_closes(engine, venue)
    out: list[dict[str, Any]] = []
    for entry in _placed_entries(engine, venue):
        keys = _entry_keys(entry)
        if keys & resting and entry["state"] == "OPEN":
            continue
        closed_at, rested_through = closes.get(asset_of(entry["symbol"]), ("", frozenset()))
        if entry["created_at"] <= closed_at and not keys & rested_through:
            continue
        out.append(entry)
    return out


def journal_positions(
    engine: Engine, open_orders: list[dict[str, Any]], *, venue: str
) -> list[dict[str, Any]]:
    """What the journal believes is open: the sizes of `current_entries` per (asset, side).

    The input `exchange/reconcile.py` needs on its "ours" side (review X-N2). Sizes are what was
    ordered; a partial fill or a trim shows up as `size_drift`, which is a warning, never an
    action.
    """
    book: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in current_entries(engine, open_orders, venue=venue):
        key = (asset_of(entry["symbol"]), entry["side"])
        held = book.setdefault(
            key,
            {
                "symbol": entry["symbol"],
                "side": entry["side"],
                "size": Decimal("0"),
                "state": "OPEN",
                "plan_id": entry["plan_id"],
            },
        )
        held["size"] += entry["size"]
        held["plan_id"] = entry["plan_id"]
    return list(book.values())


def stop_for(position: dict[str, Any], plan_stop: Decimal | None, atr: Decimal | None) -> Decimal:
    """The stop to repair with: the plan's, else 2×ATR14 from the entry (ARCHITECTURE.md §8).

    The ATR stop is clamped to half the isolated-margin liquidation distance (≈ entry/leverage).
    Without the clamp a volatile asset produced a negative stop (rejected, so the repair failed)
    or — worse — a positive stop beyond liquidation that OKX accepts and that can never fire.
    Half the distance also guarantees the stop stays above zero for any leverage ≥ 1.
    """
    return repair_stop(position, plan_stop, atr)[0]


def repair_stop(
    position: dict[str, Any], plan_stop: Decimal | None, atr: Decimal | None
) -> tuple[Decimal, str]:
    """`stop_for`, plus the honest name of the rule that set it (review X-17).

    The alert used to say "2xATR14" whatever happened — also when no ATR was available and the
    percentage proxy was used, and when the liquidation clamp rather than the ATR decided the
    distance. Note that the proxy is 2% of entry *standing in for the ATR*, so the stop sits
    2 x 2% = 4% away; the label says so rather than hide it (the number itself is a [tune]
    question, not a bug fix).
    """
    if plan_stop is not None:
        return plan_stop, "plan"
    entry = Decimal(str(position["entry_price"]))
    source = "2xATR14"
    if atr is None or atr <= 0:
        atr = entry * Decimal("0.02")  # last resort: 2% of entry as the ATR proxy
        source = "2x(2% of entry) - no ATR available"
    leverage = max(Decimal(str(position.get("leverage") or 1)), Decimal("1"))
    max_offset = entry / leverage * LIQUIDATION_CLAMP
    offset = DEFAULT_STOP_ATR_MULT * atr
    if offset > max_offset:
        offset = max_offset
        source = f"half the liquidation distance at {leverage}x (tighter than {source})"
    side = _position_side(str(position["side"]))
    return (entry - offset if side == "long" else entry + offset), source


def atr_from_ohlcv(candles: list[list[float]], period: int = 14) -> Decimal | None:
    """True range average over the last `period` closed candles.

    The last row is the bar still forming — OKX returns it unconfirmed — and it used to be
    averaged in, so a spike in progress could move the emergency stop (review X-17). It is
    dropped. (A simple average, not Wilder's smoothing; changing that changes the number.)
    """
    candles = candles[:-1]
    if len(candles) < period + 1:
        return None
    trs: list[Decimal] = []
    for previous, current in zip(candles[-period - 1 :], candles[-period:], strict=False):
        high, low, prev_close = (
            Decimal(str(current[2])),
            Decimal(str(current[3])),
            Decimal(str(previous[4])),
        )
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / Decimal(len(trs)) if trs else None


class Watchdog:
    def __init__(
        self,
        exchange: ExchangeAdapter,
        engine: Engine,
        notifier: Notifier,
        settings: Settings | None = None,
        breakers: BreakerRegistry | None = None,
        plan_stops: PlanStops | None = None,
    ) -> None:
        self.exchange = exchange
        self.engine = engine
        self.notifier = notifier
        self.settings = settings or get_settings()
        self.breakers = breakers or BreakerRegistry()
        # None: read from the journal on each pass, against the venue's book (review 2026-10-07).
        self._given_plan_stops = plan_stops
        self.plan_stops: PlanStops = plan_stops or {}
        self._journal_errors: list[str] = []

    # --- steps -----------------------------------------------------------
    def _mark(self, symbol: str) -> Decimal | None:
        try:
            last = self.exchange.fetch_ticker(symbol).get("last")
            return None if last is None else Decimal(str(last))
        except Exception as exc:  # noqa: BLE001 - no mark means we fall back to placing the stop
            log.warning("watchdog.mark_failed", symbol=symbol, error=venue_error(exc))
            return None

    def _plan_stop(self, symbol: str, position: dict[str, Any]) -> Decimal | None:
        """The journalled stop for this position's own (asset, side), if it is on the right side.

        A long's stop must sit below its entry and a short's above (review X-N3). One on the
        wrong side can only be a journal that disagrees with the venue — a flipped position, a
        hand-opened one — and repairing with it would close the position at market on the spot.
        The ATR stop is the safer answer to a number we cannot explain.

        Nor may it sit further out than the ATR stop is allowed to (review 2026-10-07):
        `LIQUIDATION_CLAMP` of the liquidation distance at the position's leverage. The gate
        never approves a stop that far out (it keeps liquidation at 2.5x the stop distance or
        more), so one there belongs to some other trade, and past liquidation it can never fire.
        """
        side = _position_side(str(position["side"]))
        stop = self.plan_stops.get((asset_of(symbol), side))
        if stop is None:
            return None
        entry = Decimal(str(position["entry_price"]))
        if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
            log.warning("watchdog.plan_stop_wrong_side", symbol=symbol, side=side, stop=str(stop))
            return None
        leverage = max(Decimal(str(position.get("leverage") or 1)), Decimal("1"))
        if abs(entry - stop) > entry / leverage * LIQUIDATION_CLAMP:
            log.warning("watchdog.plan_stop_too_far", symbol=symbol, side=side, stop=str(stop))
            return None
        return stop

    def _close_past_stop(self, run_id: str, symbol: str, stop: Decimal, mark: Decimal) -> bool:
        """The market is already past where the stop belongs — exit now, do not queue an order."""
        try:
            self.exchange.close(symbol)
        except Exception as exc:  # noqa: BLE001 - P1 incident, must alert not crash
            self._event(
                run_id,
                symbol,
                "protection",
                "NONE",
                "NONE",
                f"emergency close failed: {venue_error(exc)}",
            )
            self.notifier.repair_failed(symbol, f"close past stop failed: {type(exc).__name__}")
            return False
        self._event(run_id, symbol, "protection", "NONE", "CLOSED", f"mark {mark} past stop {stop}")
        self.notifier.send(
            f"position closed: {symbol} mark {mark} is already past the repair stop {stop} — "
            "an exchange stop there would have been rejected and the position left naked"
        )
        return True

    def _repair(self, run_id: str, symbol: str, position: dict[str, Any]) -> str:
        """Attach a stop to an unprotected position. Bypasses an open breaker on purpose.

        Returns "repaired", "closed" or "failed". The stop is compared against the current mark
        first (review 2026-09-18, X-2): `stop_for` derives its level from the entry price, so once
        price has crossed it — exactly when protection matters most — OKX rejects the order, the
        repair fails, and every following tick retries the same impossible order while the
        position sits naked.
        """
        breaker = self.breakers.get("exchange")
        breaker.allow(force=True)
        plan_stop = self._plan_stop(symbol, position)
        atr: Decimal | None = None
        if plan_stop is None:
            try:
                atr = atr_from_ohlcv(self.exchange.fetch_ohlcv(symbol, "4h", 30))
            except Exception as exc:  # noqa: BLE001 - fall back to the percentage stop
                log.warning("watchdog.atr_failed", symbol=symbol, error=venue_error(exc))
        stop, source = repair_stop(position, plan_stop, atr)
        mark = self._mark(symbol)
        if mark is not None and _stop_already_breached(position["side"], stop, mark):
            return "closed" if self._close_past_stop(run_id, symbol, stop, mark) else "failed"
        try:
            # A new full-position stop, never an amend (review 2026-10-07): the stops already on
            # the instrument may belong to other entries of the same position, and moving the
            # first one found to this level can widen it.
            self.exchange.place_stop(symbol, stop)
        except Exception as exc:  # noqa: BLE001 - P1 incident, must alert not crash
            breaker.record_failure()
            # Redacted (review S-3): the ccxt message is OKX's body, which can name the VPS's
            # address; this reason is a hash-chained row and the alert goes to a chat.
            self._event(
                run_id, symbol, "protection", "NONE", "NONE", f"repair failed: {venue_error(exc)}"
            )
            self.notifier.repair_failed(symbol, venue_error(exc))
            return "failed"
        breaker.record_success()
        self._event(run_id, symbol, "protection", "NONE", "SL_ATTACHED", f"repaired from {source}")
        self.notifier.unprotected_repaired(symbol, str(stop), source)
        return "repaired"

    def _event(
        self, run_id: str, symbol: str, machine: str, src: str | None, dst: str, reason: str
    ) -> None:
        """Journal a protection event — best effort, never at the cost of the safety action.

        Review X-N6: a "database is locked" here, after an `amend_stop` the venue had accepted,
        escaped `_repair` and aborted the pass. The stop was live, the run reported it failed,
        the healthcheck ping and alert dispatch never ran, and positions after it were never
        looked at. The venue is the record that matters; a lost journal row is logged and noted.
        """
        try:
            ledger.append(
                self.engine,
                "position_events",
                {
                    "run_id": run_id,
                    "symbol": symbol,
                    "machine": machine,
                    "from_state": src,
                    "to_state": dst,
                    "reason": reason,
                },
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.error("watchdog.journal_failed", symbol=symbol, to_state=dst, error=str(exc))
            self._journal_errors.append(f"journal write failed for {symbol} {dst}: {exc}")

    def _margin_check(self, result: WatchdogResult, positions: list[dict[str, Any]]) -> None:
        for position in positions:
            ratio = position.get("margin_ratio")
            if ratio is None:
                continue
            try:
                ratio = Decimal(str(ratio))
            except ArithmeticError:
                result.notes.append(f"margin ratio unreadable on {position.get('symbol')}: {ratio}")
                continue
            if ratio < MARGIN_RATIO_FLOOR:
                result.margin_warnings.append(position["symbol"])
                self.notifier.margin_warning(position["symbol"], str(ratio))

    def _drawdown_check(
        self,
        run_id: str,
        result: WatchdogResult,
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
    ) -> None:
        # Each read is guarded on its own (review X-N6, X-11): a balance or journal error used to
        # abort the pass after the repairs, skipping the healthcheck ping and alert dispatch.
        try:
            equity = Decimal(str(self.exchange.fetch_balance()["equity"]))
        except Exception as exc:  # noqa: BLE001 - reported; the kill-switch did not evaluate
            log.error("watchdog.balance_failed", error=venue_error(exc))
            result.errors.append(f"drawdown check skipped, balance unreadable: {venue_error(exc)}")
            return
        try:
            record_equity(self.engine, run_id, equity)
        except Exception as exc:  # noqa: BLE001 - the reading still counts for this tick
            log.error("watchdog.equity_journal_failed", error=str(exc))
            result.notes.append(f"equity snapshot not journalled: {exc}")
        try:
            peak = equity_peak_30d(self.engine, equity, venue=self.settings.venue)
        except Exception as exc:  # noqa: BLE001 - no history, no verdict
            log.error("watchdog.equity_history_failed", error=str(exc))
            result.errors.append(f"drawdown check skipped, equity history unreadable: {exc}")
            return
        if peak <= 0:
            return
        drawdown = (peak - equity) / peak * Decimal("100")
        result.drawdown_pct = drawdown
        if drawdown < DD_HALT_PCT:
            self._note_provisional_halt(result, drawdown)
            return
        # Two reads before a flatten (review X-3). One glitchy balance payload must not close
        # every position at market and write HALT; a real drawdown is still there a moment later.
        try:
            second = Decimal(str(self.exchange.fetch_balance()["equity"]))
        except Exception as exc:  # noqa: BLE001 - unconfirmed is not confirmed
            log.error("watchdog.balance_recheck_failed", error=venue_error(exc))
            result.errors.append(
                f"drawdown {drawdown:.1f}% unconfirmed, re-read failed: {venue_error(exc)}"
            )
            self._provisional_halt(result, drawdown)
            return
        peak = max(peak, second)
        recheck = (peak - second) / peak * Decimal("100")
        if recheck < DD_HALT_PCT:
            result.notes.append(
                f"drawdown {drawdown:.1f}% not confirmed by a second read ({recheck:.1f}%)"
            )
            result.drawdown_pct = recheck
            self._note_provisional_halt(result, recheck)
            return
        equity, drawdown = second, recheck
        result.drawdown_pct = drawdown
        result.killed = True
        # Order matters (review X-4). HALT is written first: a crash or a systemd timeout during
        # the closes — N x 20s ccxt timeouts is easy to reach — used to leave no HALT behind, and
        # the manager kept opening positions into the drawdown that had just triggered a flatten.
        # Guarded too (review 2026-10-07): a full disk or an unwritable halt dir raised out of
        # here, and the flatten and the alert below never ran — on this tick or any later one.
        try:
            write_halt(
                self.settings.halt_file, f"{KILL_MARKER} drawdown {drawdown:.1f}% from 30d high"
            )
        except Exception as exc:  # noqa: BLE001 - the flatten must still run
            log.error("watchdog.halt_write_failed", error=str(exc))
            result.errors.append(f"HALT could not be written, new entries are not stopped: {exc}")
        try:
            ledger.append(
                self.engine,
                "kill_switches",
                {
                    "run_id": run_id,
                    "scope": "portfolio",
                    "rule": f"equity_dd_from_30d_high >= {DD_HALT_PCT}%",
                    "triggered": 1,
                    "detail": f"equity={equity} peak={peak} dd={drawdown:.2f}%",
                },
            )
        except Exception as exc:  # noqa: BLE001 - HALT is on disk; the flatten must still run
            log.error("watchdog.kill_journal_failed", error=str(exc))
            result.notes.append(f"kill-switch not journalled: {exc}")
        self._flatten(run_id, result, positions, open_orders)
        self.notifier.drawdown_halt(f"{drawdown:.1f}", str(equity), str(peak))

    def _provisional_halt(self, result: WatchdogResult, drawdown: Decimal) -> None:
        """Stop new entries on a breach the second read could not confirm (review 2026-10-07).

        Returning with only an error fails open: nothing else stops the manager opening positions
        into a drawdown, and a venue flaky enough to fail the re-read is common in a crash. Only
        the irreversible close-all waits for confirmation. An existing HALT is left as it is.
        """
        path = self.settings.halt_file
        try:
            if read_halt(path) is not None:
                return
            write_halt(
                path,
                f"{DD_UNCONFIRMED_MARKER} drawdown {drawdown:.1f}% from 30d high, second read "
                "failed — new entries stopped, nothing closed",
            )
        except Exception as exc:  # noqa: BLE001 - reported; the pass is already not ok
            log.error("watchdog.halt_write_failed", error=str(exc))
            result.errors.append(f"provisional HALT could not be written: {exc}")
            return
        self.notifier.send(
            f"⚠️ HALT written: drawdown {drawdown:.1f}% from the 30d high on one balance read; "
            "the confirming read failed. New entries are stopped, nothing was closed. The next "
            "ticks re-check — a confirmed breach still flattens; otherwise remove HALT by hand."
        )

    def _note_provisional_halt(self, result: WatchdogResult, drawdown: Decimal) -> None:
        """A provisional HALT is never lifted by the watchdog; say when the reads look fine."""
        halt = read_halt(self.settings.halt_file)
        if halt and DD_UNCONFIRMED_MARKER in halt:
            result.notes.append(
                f"provisional HALT from an unconfirmed drawdown is still in place; drawdown now "
                f"{drawdown:.1f}% — clear it by hand if the account is as it should be"
            )

    def _flatten(
        self,
        run_id: str,
        result: WatchdogResult,
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]] | None = None,
        asset: str | None = None,
    ) -> None:
        """Cancel every resting order, then close every position. Alerts on each failure.

        Cancelling first matters (review X-4): OKX's `autoCxl` only covers the closing orders
        attached to a position, so an unfilled limit entry would survive the flatten and open a
        brand-new position minutes after the kill-switch fired.

        The cancel covers every symbol in the book, not only the ones holding a position (review
        X-N4). Walking positions alone left a limit entry resting on a flat symbol alive through
        the HALT, free to fill an hour later into the drawdown that had just fired the switch.
        The watchdog passes the book it already read; `argus flatten` passes none and it is read
        here. `asset` narrows the cancels to one instrument for `argus flatten --symbol`.
        """
        if open_orders is None:
            try:
                open_orders = self.exchange.fetch_open_orders()
            except Exception as exc:  # noqa: BLE001 - the closes must still happen
                log.error("watchdog.open_orders_failed", error=str(exc))
                result.notes.append(f"could not list resting orders: {exc}")
                self.notifier.send(
                    "kill-switch: could not list resting orders — an entry on a symbol with no "
                    "position may still be live; check OKX"
                )
                open_orders = []
        symbols = list(
            dict.fromkeys(
                [
                    str(o["symbol"])
                    for o in open_orders
                    if o.get("symbol") and (asset is None or asset_of(str(o["symbol"])) == asset)
                ]
                + [str(p["symbol"]) for p in positions]
            )
        )
        for symbol in symbols:
            try:
                self.exchange.cancel_all(symbol)
                result.cancelled.append(symbol)
            except Exception as exc:  # noqa: BLE001 - a stuck cancel must not stop the closes
                log.error("watchdog.cancel_failed", symbol=symbol, error=venue_error(exc))
                result.notes.append(f"cancel failed {symbol}: {venue_error(exc)}")
                self.notifier.send(f"kill-switch: could not cancel resting orders on {symbol}")
        for position in positions:
            symbol = position["symbol"]
            try:
                self.exchange.close(symbol)
            except Exception as exc:  # noqa: BLE001 - keep closing the rest, alert on each
                log.error("watchdog.close_failed", symbol=symbol, error=venue_error(exc))
                result.close_failures.append(symbol)
                result.notes.append(f"close failed {symbol}: {venue_error(exc)}")
                self.notifier.send(
                    f"kill-switch: FAILED to close {symbol} ({type(exc).__name__}) — "
                    "the position is still open and will be retried on the next tick"
                )

    def _unreachable(
        self, run_id: str, result: WatchdogResult, detail: str, in_process: int
    ) -> None:
        """Count this tick as unreachable and alert once the streak reaches the threshold."""
        result.unreachable = True
        try:
            result.unreachable_streak = unreachable_streak(
                self.engine, run_id, reachable=False, detail=detail
            )
        except Exception as exc:  # noqa: BLE001 - fall back to what this process saw
            log.error("watchdog.reachability_journal_failed", error=str(exc))
            result.unreachable_streak = in_process
        if result.unreachable_streak >= UNREACHABLE_ALERT_AFTER:
            self.notifier.exchange_unreachable(result.unreachable_streak)

    def _reconcile_journal(
        self,
        run_id: str,
        result: WatchdogResult,
        positions: list[dict[str, Any]],
        open_orders: list[dict[str, Any]],
    ) -> None:
        """Journal versus venue: ghosts, orphans and size drift (review X-N2).

        `exchange/reconcile.py` existed and was never called; the adapter's `reconcile()` only
        answers "which positions lack a stop", which the repair loop already acts on — so every
        venue row is passed as protected here and `unprotected` never doubles up. An orphan (real
        money with no accepted entry behind it: a placement that timed out but filled, a
        hand-opened position) is journalled and paged; ghosts and drift are bookkeeping and go to
        the notes.
        """
        try:
            ours = journal_positions(self.engine, open_orders, venue=self.settings.venue)
        except Exception as exc:  # noqa: BLE001 - a bookkeeping check never blocks the pass
            result.notes.append(f"journal reconcile skipped: {type(exc).__name__}: {exc}")
            return
        venue = [
            {**p, "side": _position_side(str(p.get("side"))), "protected": True} for p in positions
        ]
        for found in journal_reconcile(ours, venue).discrepancies:
            result.discrepancies.append(f"{found.kind} {found.symbol}: {found.detail}")
            if found.kind != "orphan" or self._orphan_recently_reported(found.symbol):
                continue
            self._event(run_id, found.symbol, "position", None, "ORPHAN", found.detail)
            self.notifier.send(
                f"🚨 <b>Orphan position</b> {found.symbol}\nOn OKX with no accepted entry in the "
                "journal — no plan, no thesis, no post-mortem. Check whether a placement timed "
                "out and filled, or whether it was opened by hand."
            )

    def _orphan_recently_reported(self, symbol: str) -> bool:
        """Paged for this account in the last ORPHAN_REALERT. Another account's page is not this
        one's: a demo orphan on the symbol held a live orphan's page back for a day (F-150)."""
        cutoff = (datetime.now(UTC) - ORPHAN_REALERT).replace(microsecond=0).isoformat()
        try:
            with self.engine.connect() as conn:
                hit = conn.execute(
                    text(
                        "SELECT 1 FROM position_events e JOIN runs r ON r.run_id = e.run_id"
                        " WHERE e.machine = 'position' AND e.to_state = 'ORPHAN'"
                        " AND e.symbol = :s AND e.created_at >= :c AND r.config_hash = :venue"
                        " LIMIT 1"
                    ),
                    {"s": symbol, "c": cutoff, "venue": self.settings.venue},
                ).first()
        except Exception:  # noqa: BLE001 - unsure means page
            return False
        return hit is not None

    def _ping_healthcheck(self, result: WatchdogResult) -> None:
        url = self.settings.healthchecks_url
        if not url:
            return
        target = url if result.ok else f"{url.rstrip('/')}/fail"
        try:
            httpx.get(target, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - monitoring must not break the watchdog
            log.warning("watchdog.healthcheck_failed", error=str(exc))

    # --- entry point -----------------------------------------------------
    def run_once(self, run_id: str) -> WatchdogResult:
        """Root span, then the alert rules that read the finished result.

        `positions_unprotected` is only published when reconcile succeeded: reporting 0 after an
        unreachable exchange would turn "we cannot see the account" into "everything is fine".
        """
        result = WatchdogResult(run_id=run_id)
        self._journal_errors = []
        with spans.span("watchdog.run", run_id=run_id):
            try:
                self._run_once(run_id, result)
            except Exception as exc:  # noqa: BLE001 - the ping and the alerts below must run
                # Review X-N6/X-11: anything that escapes a step used to end the pass here, with
                # no healthcheck ping and no alert dispatch — silence that reads like a dead box.
                log.error("watchdog.pass_aborted", error=venue_error(exc))
                result.errors.append(f"pass aborted: {venue_error(exc)}")
        result.notes.extend(self._journal_errors)
        self._ping_healthcheck(result)
        if not result.unreachable:
            metrics.gauge(metrics.POSITIONS_UNPROTECTED, len(result.repair_failures))
        try:
            last_manager = alerts.last_run_at(self.engine, venue=self.settings.venue)
        except Exception as exc:  # noqa: BLE001 - a locked journal must not cost the alerts
            log.error("watchdog.last_run_failed", error=str(exc))
            last_manager = None
        alerts.dispatch(
            [
                alerts.unprotected_positions(len(result.repair_failures)),
                # Only once the streak is real: a single maintenance blip on one algo type is
                # routine venue weather (F-140) and paging for it teaches the operator to ignore
                # the page that matters.
                alerts.breaker_open(
                    ["exchange"]
                    if result.unreachable and result.unreachable_streak >= UNREACHABLE_ALERT_AFTER
                    else []
                ),
                alerts.stale_manager(
                    last_manager, datetime.now(UTC), self.settings.manager_max_gap_hours
                ),
            ],
            notifier=self.notifier,
            engine=self.engine,  # per-key cooldown: 72 ticks a day must not mean 72 messages (O-3)
        )
        return result

    def _run_once(self, run_id: str, result: WatchdogResult) -> WatchdogResult:
        halt = read_halt(self.settings.halt_file)
        if halt:
            result.halted = True
            result.notes.append(f"HALT present: {halt}")

        breaker = self.breakers.get("exchange")
        if not breaker.allow():
            result.notes.append("exchange breaker open — reconcile skipped")
            self._unreachable(run_id, result, "breaker open", breaker.attempts)
            return result
        try:
            state = self.exchange.reconcile()
            breaker.record_success()
        except Exception as exc:  # noqa: BLE001 - alert only; exchange stops still protect
            breaker.record_failure()
            result.notes.append(f"reconcile failed: {venue_error(exc)}")
            self._unreachable(run_id, result, type(exc).__name__, breaker.attempts)
            return result
        try:
            unreachable_streak(self.engine, run_id, reachable=True)
        except Exception as exc:  # noqa: BLE001 - bookkeeping; the pass goes on
            result.notes.append(f"reachability not journalled: {exc}")

        positions: list[dict[str, Any]] = state["positions"]
        open_orders: list[dict[str, Any]] = list(state.get("open_orders") or [])
        result.positions = len(positions)
        by_symbol = {p["symbol"]: p for p in positions}

        # Write down what the venue showed, and notice what stopped being there. A stop filling
        # raises no event anywhere — the row simply stops appearing — so this comparison is the
        # only close signal the system has, and a close nobody notices is a trade the Reflector
        # never learns from (F-132).
        try:
            closed = positions_journal.newly_closed(
                self.engine, positions, venue=self.settings.venue
            )
            positions_journal.record(
                self.engine, run_id, positions, set(state.get("unprotected", []))
            )
            if closed:
                positions_journal.mark_closed(
                    self.engine,
                    run_id,
                    closed,
                    realised=positions_journal.venue_realised(self.exchange, closed),
                    resting=resting_through([c.symbol for c in closed], open_orders),
                )
                result.notes.append(f"closed since last tick: {[c.symbol for c in closed]}")
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never block a repair
            result.notes.append(f"position journal failed: {type(exc).__name__}: {exc}")

        if self._given_plan_stops is None:
            try:
                self.plan_stops = plan_stops_from_journal(
                    self.engine, open_orders, venue=self.settings.venue
                )
            except Exception as exc:  # noqa: BLE001 - a repair without one uses the ATR stop
                log.error("watchdog.plan_stops_failed", error=str(exc))
                result.notes.append(f"plan stops unreadable, repairs use the ATR stop: {exc}")
                self.plan_stops = {}

        for symbol in state.get("unprotected", []):
            position = by_symbol.get(symbol)
            if position is None:
                continue
            try:
                outcome = self._repair(run_id, symbol, position)
            except Exception as exc:  # noqa: BLE001 - one bad row must not strand the rest
                # An unreadable venue row (no entry price, a non-numeric field) used to raise
                # out of the loop and leave every later unprotected position naked (X-11).
                log.error("watchdog.repair_crashed", symbol=symbol, error=venue_error(exc))
                self.notifier.repair_failed(symbol, f"repair crashed: {type(exc).__name__}")
                outcome = "failed"
            if outcome == "repaired":
                result.repaired.append(symbol)
            elif outcome == "closed":
                result.closed_past_stop.append(symbol)
            else:
                result.repair_failures.append(symbol)

        self._reconcile_journal(run_id, result, positions, open_orders)
        self._margin_check(result, positions)
        if not result.halted or (halt and DD_UNCONFIRMED_MARKER in halt):
            # A provisional HALT stops entries only; the breach it stands for is still judged.
            self._drawdown_check(run_id, result, positions, open_orders)
        elif halt and KILL_MARKER in halt and (positions or open_orders):
            # X-4: a halted tick used to skip this branch entirely, so a position that refused to
            # close once was never closed at all — the kill-switch reported success while the risk
            # stayed open. An operator's own `touch HALT` never reaches here. A resting entry
            # whose cancel failed is retried too, even once every position is gone (X-N4).
            result.notes.append("retrying an unfinished kill-switch flatten")
            self._flatten(run_id, result, positions, open_orders)
        log.info(
            "watchdog.done",
            positions=result.positions,
            repaired=result.repaired,
            failures=result.repair_failures,
            killed=result.killed,
        )
        return result
