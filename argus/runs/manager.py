"""MANAGER run — regime → universe → dossiers → proposal → Risk Gate → journal.

In shadow mode (all that exists today) nothing is placed: the run researches, gates and records,
so the decisions can be reviewed and replayed before any capital is involved. Order placement
arrives in Phase 4.

Every collaborator is injected (`ManagerDeps`), which is what lets the whole pipeline be tested
offline. One asset failing never aborts the run — its error is recorded and the rest proceed.
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import Engine, text

from argus.config import get_settings, risk_config
from argus.data.parallel import run_parallel
from argus.exchange.errors import InstrumentNotListedError
from argus.execution.place import PLACEABLE
from argus.features.dossier import Dossier, render, token_count
from argus.features.regime import RegimeView
from argus.features.universe import Asset
from argus.gate.killswitch import KillContext, KillRule, Triggered, evaluate_plan_rules, parse_rule
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.gate.protections import TradingHistory
from argus.gate.repair import repair_stop, repair_targets
from argus.gate.risk_config import max_positions, select_profile, step_policies
from argus.gate.risk_config import parse as parse_risk_config
from argus.gate.rules import (
    GateResult,
    MarketContext,
    OpenPosition,
    PlanInput,
    PortfolioState,
    Side,
    evaluate,
    gate_error,
)
from argus.gate.sizing import StepPolicy
from argus.journal import ledger
from argus.journal.db import new_id, utc_now
from argus.llm import stub
from argus.observability import alerts, metrics, spans
from argus.runs.watchdog import write_halt

if TYPE_CHECKING:
    from argus.features.market import Quote

log = structlog.get_logger(__name__)

AssetsFn = Callable[[], list[Asset]]
RegimeFn = Callable[[list[Asset]], tuple[RegimeView, list[Any]]]
DossierFn = Callable[[Asset, RegimeView], Dossier]
MarketFn = Callable[[Asset, Dossier], MarketContext]
PortfolioFn = Callable[[], tuple[float, float, list[OpenPosition]]]
# run_id first: every LLM call has to be journaled against the run that made it, and a
# proposer that cannot name the run cannot write that row.
ProposeFn = Callable[[str, Dossier, MarketContext], PlanInput | None]
# (run_id, plan, gate, plan_id, instrument) -> journal order id, or None when nothing was placed.
ExecuteFn = Callable[[str, PlanInput, "GateResult", str, str], str | None]
# (run_id, [(asset, dossier, market)]) -> {symbol: plan}. ONE call for the whole cycle, which is
# what lets the Trader rank candidates against each other instead of judging each alone (F-117).
BatchProposeFn = Callable[[str, list[tuple[Asset, Dossier, MarketContext]]], dict[str, PlanInput]]
# A fresh mark and spread for one asset, read just before its plan is gated.
QuoteFn = Callable[[Asset], "Quote"]
# (asset, timeframe "4h" | "1d") -> close of the last CLOSED candle, or None when unavailable.
CloseFn = Callable[[str, str], float | None]


@dataclass
class ManagerDeps:
    assets: AssetsFn
    regime: RegimeFn
    dossier: DossierFn
    market: MarketFn
    portfolio: PortfolioFn
    propose: ProposeFn
    # When set, the Trader is asked ONCE per run over every researched asset instead of once per
    # asset. `propose` stays for the deterministic stub and the golden suite.
    propose_batch: BatchProposeFn | None = None
    # None means "shadow only". A live run must be handed something that can place, which makes
    # the absence of an executor a structural guarantee rather than a flag to remember.
    execute: ExecuteFn | None = None
    # Re-reads the live mark and spread immediately before `evaluate` (review G-N3 / D-1). The
    # quote in `market` was read during research, before the Trader, Red Team and Risk Debate
    # calls - minutes earlier in a 480 s run - and a market entry fills at the mark of the moment
    # it is placed. None only where `market` is already the moment (offline tests).
    quote: QuoteFn | None = None
    # Closed-candle closes for the open positions' own invalidation rules (gate 11, review G-3).
    # Required once a held position has a rule to judge: a run that cannot tell whether a switch
    # fired closes FAILED rather than gating as if none had.
    last_close: CloseFn | None = None


@dataclass
class ManagerResult:
    run_id: str
    shadow: bool
    # What the run row says when it closes. A caller decides its exit code from this, so a FAILED
    # run cannot look like a quiet success to systemd (review J-N4).
    status: str = "RUNNING"
    halted: bool = False
    regime_label: str | None = None
    dossiers: int = 0
    plans: int = 0
    approved: int = 0
    adjusted: int = 0
    rejected: int = 0
    skipped_for_gaps: int = 0
    orders_placed: int = 0
    errors: list[str] = field(default_factory=list)
    # Researched assets the armed venue does not list (demo lists fewer swaps than live): noted,
    # skipped, and never an error that fails the run.
    not_listed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.regime_label or 'no regime'} · {self.dossiers} dossiers · {self.plans} plans "
            f"({self.approved} approved, {self.adjusted} adjusted, {self.rejected} rejected)"
            + (
                f" · not listed on this venue: {', '.join(self.not_listed)}"
                if self.not_listed
                else ""
            )
        )


# Every journal row hangs off a run, and `runs.config_hash` records the venue that run traded
# (F-131). Demo and live share one journal (ARGUS_HOME), so each query of the book reads only this
# account's rows. A row of an unrecorded venue ('-', written before F-131) is not this account's.
_THIS_ACCOUNT = "JOIN runs acct ON acct.run_id = {t}.run_id WHERE acct.config_hash = :venue"


def _venue() -> str:
    return get_settings().venue


def lowest_equity_since(
    engine: Engine, since: datetime, now: datetime, equity: float
) -> float | None:
    """Lowest equity recorded from `since` to now (and now), or None if the journal starts later.

    The small-account exit needs equity at or above $500 for seven consecutive days (§5.1). A
    journal that does not reach back that far cannot vouch for the whole window.
    """
    with engine.connect() as conn:
        first = conn.execute(
            text(f"SELECT MIN(e.created_at) FROM equity_snapshots e {_THIS_ACCOUNT.format(t='e')}"),
            {"venue": _venue()},
        ).scalar()
        rows = conn.execute(
            text(
                "SELECT e.created_at, e.equity FROM equity_snapshots e"
                f" {_THIS_ACCOUNT.format(t='e')} AND e.created_at >= :c"
            ),
            {"c": since.replace(microsecond=0).isoformat(), "venue": _venue()},
        ).all()
    if first is None or _when(first) > since:
        return None
    return min([equity, *(float(value) for at, value in rows if _when(at) <= now)])


def risk_policy(
    engine: Engine, equity: float, now: datetime
) -> tuple[str, tuple[StepPolicy, ...], int]:
    """(profile, steps, max positions) from the validated risk file (review G-N5 / G-17 / G-5).

    The file is parsed strictly on every run: a typo, an impossible value or a pinned number that
    disagrees with the code raises here, and the run closes FAILED before any research.
    """
    config = parse_risk_config(risk_config())
    window = timedelta(days=config.small_account_exit_days)
    profile = select_profile(
        config,
        equity,
        override=get_settings().risk_profile,
        lowest_recent_equity=lowest_equity_since(engine, now - window, now, equity),
    )
    steps = step_policies(config, profile, equity)
    return profile, steps, max_positions(config, profile, equity)


# --- the book the gate judges against (review G-N1 / G-15 / G-3) ---------------------------------
# Until these existed the manager handed `evaluate` a TradingHistory of constants, `positions=[]`
# and `peak_equity_30d = equity`, so the heat, protection, duplicate and downside gates judged every
# plan against an empty, flat book while the same loop placed orders.

WEEK = timedelta(days=7)
PEAK_WINDOW = timedelta(days=30)
RECENT_ROWS = 500  # journal rows read per query; far more than a week of closes or entries
DOWNSIDE_WINDOW = 20  # DECISION_LOGIC §6 gate 14: the rolling 20 closed trades ...
DOWNSIDE_MISS_R = -1.3  # ... whose realised loss went past 1.3R
# Entry orders that may still fill. NEW counts too: a row written before the venue call whose
# process died may be live at the venue, and counting it is the side that cannot overtrade.
PENDING_ORDER_STATES = ("NEW", "OPEN", "PARTIAL")


def _asset(symbol: Any) -> str:
    """`BTC/USDT:USDT` (ccxt), `BTC-USDT-SWAP` (OKX) and `BTC` (plans) are one asset."""
    return str(symbol).split("/")[0].split("-")[0].upper()


def _side(value: Any) -> Side:
    side = str(value).lower()
    if side in ("long", "buy"):
        return "long"
    if side in ("short", "sell"):
        return "short"
    raise ValueError(f"unknown position side {value!r}")


def _when(value: Any) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _planned_losses(engine: Engine) -> list[tuple[datetime, str, Side, float]]:
    """(written_at, asset, side, approved risk) of this account's entry orders, newest first."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT o.created_at, p.symbol, p.side, g.reasons FROM orders o"
                " JOIN plans p ON p.plan_id = o.plan_id"
                " JOIN gate_results g ON g.plan_id = o.plan_id"
                f" {_THIS_ACCOUNT.format(t='o')}"
                " AND o.kind = 'entry' ORDER BY o.created_at DESC LIMIT :n"
            ),
            {"n": RECENT_ROWS, "venue": _venue()},
        ).all()
    out: list[tuple[datetime, str, Side, float]] = []
    for written_at, symbol, side, reasons in rows:
        risk = (json.loads(reasons).get("sized") or {}).get("risk_usd")
        if risk is not None and math.isfinite(float(risk)):
            out.append((_when(written_at), _asset(symbol), _side(side), float(risk)))
    return out


def _closes(engine: Engine, since: datetime | None) -> list[tuple[datetime, str, float | None]]:
    """(closed_at, asset, pnl_usd or None when unknown) since `since`, oldest first.

    A close raises no event at the venue - the position just stops appearing (F-132) - and the
    last snapshot of it is up to a watchdog tick old. A long seen at +0.1R that stopped out at -1R
    before the next look was booked as a win. So a close is worth, in order:
    1. what the venue booked for it (`realised_pnl`, written when the close was noticed);
    2. the loss at the plan stop - the approved risk of the entry behind it - when the venue
       could not say;
    3. unknown (None), when neither exists. Never the stale unrealised.
    """
    window = "" if since is None else " AND c.created_at >= :cutoff"
    params: dict[str, Any] = {"n": RECENT_ROWS, "venue": _venue()}
    if since is not None:
        params["cutoff"] = since.replace(microsecond=0).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.created_at, c.symbol, c.side, json_extract(c.raw, '$.realised_pnl')"
                f" FROM positions_snapshots c {_THIS_ACCOUNT.format(t='c')}"
                f" AND c.state = 'CLOSED'{window}"
                " ORDER BY c.created_at DESC LIMIT :n"
            ),
            params,
        ).all()
    planned: list[tuple[datetime, str, Side, float]] | None = None
    out: list[tuple[datetime, str, float | None]] = []
    for closed_at, symbol, side, realised in reversed(rows):
        at, asset = _when(closed_at), _asset(symbol)
        try:
            pnl: float | None = None if realised is None else float(realised)
        except (TypeError, ValueError):
            pnl = None
        if pnl is not None and not math.isfinite(pnl):
            pnl = None
        if pnl is None:
            if planned is None:
                planned = _planned_losses(engine)
            try:
                closed_side: Side | None = _side(side)
            except ValueError:
                closed_side = None
            risk = next(
                (
                    r
                    for written, a, s, r in planned
                    if a == asset and s == closed_side and written <= at
                ),
                None,
            )
            pnl = None if risk is None else -risk
        out.append((at, asset, pnl))
    return out


def closed_trades(engine: Engine, now: datetime) -> list[tuple[datetime, str, float]]:
    """(closed_at, asset, pnl_usd) of the closes in the last week, oldest first.

    A close whose PnL cannot be read raises: the loss limits cannot be judged without it, and
    guessing zero is the guess that lets a losing day trade on. Only the week the loss limits
    read is held to that, so one bad row cannot stop the manager for ever.
    """
    out: list[tuple[datetime, str, float]] = []
    for closed_at, asset, pnl in _closes(engine, now - WEEK):
        if pnl is None:
            raise ValueError(f"closed position on {asset} at {closed_at} has no recorded PnL")
        out.append((closed_at, asset, pnl))
    return out


def _entry_times(engine: Engine) -> list[datetime]:
    """When each entry order was written - attempts, not fills (F-029)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT o.created_at FROM orders o {_THIS_ACCOUNT.format(t='o')}"
                " AND o.kind = 'entry' ORDER BY o.created_at DESC LIMIT :n"
            ),
            {"n": RECENT_ROWS, "venue": _venue()},
        ).all()
    return [_when(row[0]) for row in rows]


def trading_history(
    engine: Engine, equity: float, now: datetime, limits: DecisionLimits = LIVE_CONSERVATIVE
) -> TradingHistory:
    """Gate 9's inputs from the journal: realised PnL, the losing streak, entries made.

    Percentages are of *current* equity, which after a loss is the smaller number - the side that
    trips a loss limit sooner rather than later.
    """
    if not equity > 0:
        raise ValueError(f"equity {equity!r} cannot express PnL as a share of the account")
    closed = [trade for trade in closed_trades(engine, now) if trade[0] <= now]
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily = sum(pnl for at, _, pnl in closed if at >= day_start)
    weekly = sum(pnl for at, _, pnl in closed if at >= now - WEEK)
    # The streak and its pause have no window (DECISION_LOGIC gate 9): three losses spread over
    # more than a week are still three in a row. Read with no cutoff, and a close whose PnL
    # cannot be read counts as a loss here - it may neither fail the run nor end a streak.
    history = [(at, pnl is None or pnl < 0) for at, _, pnl in _closes(engine, None) if at <= now]
    streak = 0
    for _, lost in reversed(history):
        if not lost:
            break
        streak += 1
    last_loss_at = next((at for at, lost in reversed(history) if lost), None)
    # The loss that last took a streak to the pause limit (G-13b): the pause and the reduced
    # trades after it run from here, even once a win has ended the streak.
    pause_started_at = None
    run = 0
    for at, lost in history:
        run = run + 1 if lost else 0
        if run >= limits.consecutive_loss_pause:
            pause_started_at = at
    stop_outs: dict[str, list[datetime]] = {}
    for at, asset, pnl in closed:
        if pnl < 0 and now - at <= WEEK:
            stop_outs.setdefault(asset, []).append(at)
    entries = [at for at in _entry_times(engine) if at <= now]
    return TradingHistory(
        daily_pnl_pct=daily / equity * 100,
        weekly_pnl_pct=weekly / equity * 100,
        consecutive_losses=streak,
        entries_this_run=0,
        entries_today=sum(1 for at in entries if at >= day_start),
        stop_outs_by_symbol=stop_outs,
        last_loss_at=last_loss_at,
        # Entries made since the pause began. Entries are blocked during the pause, so these
        # are the trades taken since it ended - the ones the reduced size applies to.
        trades_since_pause=sum(
            1 for at in entries if pause_started_at is None or at > pause_started_at
        ),
        pause_started_at=pause_started_at,
    )


def peak_equity_30d(engine: Engine, equity: float, now: datetime) -> float:
    """Highest equity the watchdog recorded in the last 30 days, never below today's."""
    cutoff = (now - PEAK_WINDOW).replace(microsecond=0).isoformat()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT e.created_at, e.equity FROM equity_snapshots e"
                f" {_THIS_ACCOUNT.format(t='e')} AND e.created_at >= :c"
            ),
            {"c": cutoff, "venue": _venue()},
        ).all()
    seen = [float(value) for at, value in rows if _when(at) <= now]
    return max([equity, *seen])


def downside_miss_rate(engine: Engine) -> float | None:
    """Share of recent losers that lost more than 1.3R - gate 14's slippage monitor."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT m.outcome_r FROM postmortems m {_THIS_ACCOUNT.format(t='m')}"
                " AND m.outcome_r IS NOT NULL ORDER BY m.created_at DESC LIMIT :n"
            ),
            {"n": DOWNSIDE_WINDOW, "venue": _venue()},
        ).all()
    losers = [float(row[0]) for row in rows if float(row[0]) < 0]
    if not losers:
        return None
    return sum(1 for r in losers if r < DOWNSIDE_MISS_R) / len(losers)


def _plan_payload(engine: Engine, asset: str, side: Side) -> dict[str, Any] | None:
    """The plan behind the most recent entry order on this asset and side, if we placed one."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.symbol, p.side, p.payload FROM orders o"
                " JOIN plans p ON p.plan_id = o.plan_id"
                f" {_THIS_ACCOUNT.format(t='o')}"
                " AND o.kind = 'entry' ORDER BY o.created_at DESC LIMIT :n"
            ),
            {"n": RECENT_ROWS, "venue": _venue()},
        ).all()
    for symbol, plan_side, payload in rows:
        if _asset(symbol) == asset and plan_side == side:
            loaded = json.loads(payload)
            return loaded if isinstance(loaded, dict) else None
    return None


def _beta(payload: dict[str, Any] | None) -> float | None:
    value = ((payload or {}).get("market") or {}).get("beta_btc")
    return None if value is None else float(value)


def venue_stops(algos: list[dict[str, Any]]) -> dict[str, list[Decimal | None]]:
    """asset -> trigger prices of the protective algos resting at the venue.

    As `ExchangeAdapter.fetch_algo_orders` lists them. A trailing stop protects but has no fixed
    price, so it is listed as None. Take-profits do not protect and are left out.
    """
    out: dict[str, list[Decimal | None]] = {}
    for algo in algos:
        if algo.get("kind") not in ("sl", "trailing"):
            continue
        trigger = algo.get("trigger_px") if algo.get("kind") == "sl" else None
        price = None if trigger is None else Decimal(str(trigger))
        out.setdefault(_asset(algo.get("symbol")), []).append(price)
    return out


def venue_positions(
    engine: Engine,
    rows: list[dict[str, Any]],
    contract_values: dict[str, Decimal],
    stops: dict[str, list[Decimal | None]],
) -> list[OpenPosition]:
    """Positions as `ExchangeAdapter.fetch_positions` returns them, in the gate's terms.

    Risk is what the trade can still lose to its stop. A position with no plan behind it (opened
    by hand, or a plan the journal cannot find) counts its whole notional as at risk: an unknown
    stop is not a small one. So does a naked position - no stop resting at the venue (`stops`,
    from `venue_stops`). Otherwise it is the larger of the distances to the plan's stop and to
    the stop actually resting at the venue, which a repair or a widening may have moved away.
    """
    out: list[OpenPosition] = []
    for row in rows:
        size = abs(Decimal(str(row.get("size") or 0)))
        if size == 0:
            continue
        symbol = str(row["symbol"])
        contract_value = contract_values.get(symbol)
        if contract_value is None or contract_value <= 0:
            raise ValueError(f"no contract value for {symbol}: cannot size the open position")
        asset, side = _asset(symbol), _side(row["side"])
        entry = Decimal(str(row["entry_price"]))
        mark = Decimal(str(row.get("mark_price") or 0))
        quantity = size * contract_value
        notional = float(quantity * (mark if mark > 0 else entry))
        payload = _plan_payload(engine, asset, side)
        stop = None if payload is None else payload.get("stop")
        resting = stops.get(asset, [])
        risk = notional
        if stop is not None and resting:
            stop_px = Decimal(str(stop))
            if (side == "long" and stop_px < entry) or (side == "short" and stop_px > entry):
                # Loss-side distance from entry; a stop moved past entry into profit risks 0.
                loss = [
                    entry - p if side == "long" else p - entry for p in resting if p is not None
                ]
                distance = max([abs(entry - stop_px), *(d for d in loss if d > 0)])
                risk = float(quantity * distance)
        ratio = row.get("margin_ratio")
        out.append(
            OpenPosition(
                asset,
                side,
                round(notional, 2),
                round(risk, 2),
                _beta(payload),
                None if ratio is None else float(ratio),
            )
        )
    return out


def active_kill_switches(
    engine: Engine,
    positions: list[OpenPosition],
    last_close: CloseFn | None,
    real_yield_10y: float | None,
) -> list[Triggered]:
    """The open positions' own invalidation rules that have fired - gate 11's input (G-3).

    `active_kill_switches` was the constant `[]`, so "plan fights an active kill-switch" could
    never reject: with an open long's "1d close below 95" fired, another long was still approved.
    Each position's rules come from the plan that opened it, parsed into their typed form, and
    are judged on the last closed candle of the rule's own timeframe. A rule that no longer
    parses (a plan from before gate 11 parsed them) is skipped and logged: it could not fire
    before either. A rule that parses but cannot be judged - no closed candle - raises.
    """
    fired: list[Triggered] = []
    for position in positions:
        payload = _plan_payload(engine, position.symbol, position.side) or {}
        rules: list[KillRule] = []
        for raw in payload.get("invalidation_rules") or []:
            try:
                rules.append(parse_rule(raw))
            except (TypeError, ValueError) as exc:
                log.warning("manager.kill_rule_unparseable", symbol=position.symbol, error=str(exc))
        for timeframe in sorted({rule.timeframe for rule in rules}):
            price = None if last_close is None else last_close(position.symbol, timeframe)
            if price is None or not math.isfinite(price):
                raise ValueError(
                    f"no closed {timeframe} candle for {position.symbol}: cannot tell whether its"
                    " invalidation rules have fired"
                )
            ctx = KillContext(last_closed_price=price, real_yield_10y=real_yield_10y)
            group = [rule for rule in rules if rule.timeframe == timeframe]
            fired += [
                Triggered(t.rule, f"{position.symbol} {position.side}: {t.detail}", position.side)
                for t in evaluate_plan_rules(group, ctx, position.side)
            ]
    return fired


def risk_book(
    equity: float,
    positions: list[OpenPosition],
    closed: list[tuple[datetime, str, float]],
    now: datetime,
) -> dict[str, Any]:
    """The book as the Risk Debate and `features.risk_flags` read it (review D-N2 / G-N7).

    Built from the same `OpenPosition`s the gate judges, so the agent and the gate argue about one
    book. Risk is expressed as a share of equity (G-N7: the payload read a `risk_pct` attribute
    `OpenPosition` never had, so open risk always read 0). No gate threshold is put here: this
    dict is rendered into the prompt verbatim, and an agent told a limit applies it itself.
    """

    def pct(usd: float) -> float:
        return round(usd / equity * 100, 3) if equity > 0 else 0.0

    return {
        "equity_usd": round(equity, 2),
        "open_positions": [
            {
                "symbol": p.symbol,
                "side": p.side,
                "risk_pct": pct(p.risk_usd),
                "beta_btc": p.beta_btc,
            }
            for p in positions
        ],
        "open_risk_pct": pct(sum(p.risk_usd for p in positions)),
        "recent_stop_outs": [
            {"symbol": asset, "hours_ago": round((now - at).total_seconds() / 3600, 1)}
            for at, asset, pnl in closed
            if pnl < 0 and timedelta(0) <= now - at <= WEEK
        ],
    }


def _latest_order_states(engine: Engine) -> dict[str, str]:
    """Every order's current state in one pass (the state lives in `position_events`)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT json_extract(detail, '$.order_id'), to_state FROM position_events"
                " WHERE machine = 'order' ORDER BY created_at, rowid"
            )
        ).all()
    return {str(order_id): str(state) for order_id, state in rows if order_id}


def _resting(engine: Engine) -> list[OpenPosition]:
    """Every entry order that may still fill, sized as the gate approved it."""
    states = _latest_order_states(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT o.order_id, p.symbol, p.side, p.payload, g.reasons FROM orders o"
                " LEFT JOIN plans p ON p.plan_id = o.plan_id"
                " LEFT JOIN gate_results g ON g.plan_id = o.plan_id"
                f" {_THIS_ACCOUNT.format(t='o')} AND o.kind = 'entry'"
            ),
            {"venue": _venue()},
        ).all()
    out: list[OpenPosition] = []
    for order_id, symbol, side, payload, reasons in rows:
        if states.get(str(order_id), "NEW") not in PENDING_ORDER_STATES:
            continue
        if symbol is None or reasons is None:
            raise ValueError(f"entry order {order_id} has no gated plan behind it")
        sized = json.loads(reasons).get("sized") or {}
        if sized.get("risk_usd") is None or sized.get("notional_usd") is None:
            raise ValueError(f"entry order {order_id} has no recorded size")
        out.append(
            OpenPosition(
                _asset(symbol),
                _side(side),
                float(sized["notional_usd"]),
                float(sized["risk_usd"]),
                _beta(json.loads(payload)),
            )
        )
    return out


def pending_entries(engine: Engine, held: set[str]) -> list[OpenPosition]:
    """Entry orders still resting at the venue on assets not in `held`, counted as if filled.

    The resting sweep runs every twelve hours and the manager twice a day, so an order from the
    previous run is usually still live when the next one gates (review G-15). `gate_book` is the
    whole book; this is the part of it with no position yet.
    """
    return [p for p in _resting(engine) if p.symbol not in held]


def gate_book(engine: Engine, positions: list[OpenPosition]) -> list[OpenPosition]:
    """Venue positions plus every entry that may still fill - the book the gate judges.

    A held asset whose entry is still NEW, OPEN or PARTIAL counts the larger of the position and
    the approved plan. The venue reports only the filled part; the unfilled rest still rests at
    the venue, and once it fills the position is the whole plan. Taking the larger also cannot
    double-count an entry that filled completely before the sweep marked it FILLED.
    """
    book = list(positions)
    for entry in _resting(engine):
        at = next(
            (
                i
                for i, held in enumerate(book)
                if held.symbol == entry.symbol and held.side == entry.side
            ),
            None,
        )
        if at is None:
            book.append(entry)
            continue
        held = book[at]
        book[at] = OpenPosition(
            held.symbol,
            held.side,
            max(held.notional_usd, entry.notional_usd),
            max(held.risk_usd, entry.risk_usd),
            held.beta_btc if held.beta_btc is not None else entry.beta_btc,
            held.margin_ratio,
        )
    return book


def _record_regime(engine: Engine, run_id: str, view: RegimeView, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO regime_snapshots (snapshot_id, run_id, created_at, label, risk_mult,"
                " limit_only, inputs, reasons, modifiers, gaps)"
                " VALUES (:i, :r, :t, :l, :m, :lo, :in, :re, :mo, :ga)"
            ),
            {
                "i": new_id(),
                "r": run_id,
                "t": now.isoformat(),
                "l": view.label,
                "m": view.risk_mult,
                "lo": int(view.limit_only),
                "in": json.dumps(view.inputs.model_dump() if view.inputs else {}, default=str),
                "re": json.dumps(view.reasons),
                "mo": json.dumps(view.modifiers),
                "ga": json.dumps(view.data_gaps),
            },
        )


def _record_dossier(engine: Engine, run_id: str, dossier: Dossier, now: datetime) -> str:
    payload = render(dossier)
    dossier_id = new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dossiers (dossier_id, run_id, created_at, symbol, source, tokens,"
                " trade_critical, payload, gaps) VALUES (:i, :r, :t, :s, :src, :tok, :tc, :p, :g)"
            ),
            {
                "i": dossier_id,
                "r": run_id,
                "t": now.isoformat(),
                "s": dossier.symbol,
                "src": dossier.source,
                "tok": token_count(payload),
                "tc": int(dossier.trade_critical_present),
                "p": payload,
                "g": json.dumps(dossier.data_gaps),
            },
        )
    return dossier_id


def _market_snapshot(market: MarketContext) -> dict[str, Any]:
    """The gate's inputs, flattened, so `argus replay` can explain a verdict without the network.

    Only scalars: the nested regime/spec/fee objects change shape between phases, and a replay
    that cannot be read in two years is not a replay.
    """
    return {
        "mark": market.mark,
        "atr_pct": market.atr_pct,
        "spread_pct": market.spread_pct,
        "funding_8h": market.funding_8h,
        "swing_high_20": market.swing_high_20,
        "swing_low_20": market.swing_low_20,
        "consensus_ok": market.consensus_ok,
        "derivatives_age_min": market.derivatives_age_min,
        "news_present": market.news_present,
        "suspect_sources": market.suspect_sources,
        "next_event_hours": market.next_event_hours,
        "in_market_list": market.in_market_list,
        "beta_btc": market.beta_btc,
        "regime": market.regime.label,
        "regime_risk_mult": market.regime.risk_mult,
        "limit_only": market.regime.limit_only,
        "maker_fee": str(market.fees.maker),
        "taker_fee": str(market.fees.taker),
        "lot_size": str(market.spec.lot_size),
        "min_contracts": str(market.spec.min_contracts),
        "max_leverage": market.spec.max_leverage,
    }


def _record_decision(
    engine: Engine,
    run_id: str,
    plan: PlanInput,
    gate: GateResult,
    dossier_id: str,
    mode: str,
    market: MarketContext,
) -> str:
    """Plans and gate results are ledger rows — rejected ones are the calibration dataset."""
    plan_id = new_id()
    ledger.append(
        engine,
        "plans",
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "mode": mode,
            "symbol": plan.symbol,
            "side": plan.side,
            "state": "REJECTED" if gate.status == "REJECTED" else "GATED",
            "step": None if gate.sized is None else gate.sized.step,
            "payload": json.dumps(
                {
                    "entry": [plan.entry_low, plan.entry_high],
                    "entry_kind": plan.entry_kind,
                    "stop": plan.stop,
                    "targets": [[t.price, t.close_pct] for t in plan.targets],
                    "time_stop_hours": plan.time_stop_hours,
                    "scores": vars(plan.scores),
                    "invalidation_rules": plan.invalidation_rules,
                    # `journal/trades.py` reads this for the Reflector. Nothing wrote it, so
                    # every closed trade reached the learning loop with no reason attached —
                    # which is most of what there is to learn from (F-130).
                    "thesis": plan.thesis,
                    # `agents/reviewer.py` keeps the Reviewer off this family (L-13).
                    "trader_family": plan.trader_family,
                    "dossier_id": dossier_id,
                    "market": _market_snapshot(market),
                    "proposer": stub.describe(),
                },
                default=str,
            ),
        },
    )
    ledger.append(
        engine,
        "gate_results",
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "verdict": gate.status,
            "reasons": json.dumps(gate.as_dict(), default=str),
        },
    )
    return plan_id


def _research(
    deps: ManagerDeps, run_id: str, asset: Asset, view: RegimeView
) -> tuple[Dossier, MarketContext | None, PlanInput | None]:
    """All the network work for one asset, off the journal thread.

    The proposal only happens here on the per-asset path. With `propose_batch` set it is deferred
    until every asset is researched, because a Trader that has not seen the other candidates
    cannot rank against them — and because fifteen model calls from four worker threads is how a
    provider starts answering 504 (F-122).
    """
    dossier = deps.dossier(asset, view)
    if not dossier.trade_critical_present:
        return dossier, None, None
    market = deps.market(asset, dossier)
    if deps.propose_batch is not None:
        return dossier, market, None
    return dossier, market, deps.propose(run_id, dossier, market)


def run_manager(
    engine: Engine,
    deps: ManagerDeps,
    now: datetime | None = None,
    shadow: bool = True,
    halt_file: Path | None = None,
    workers: int = 4,
) -> ManagerResult:
    """Root span wrapper: one trace per run, with the stages below as its children."""
    with spans.span("manager.run", shadow=shadow, workers=workers):
        return _run_manager(engine, deps, now, shadow, halt_file, workers)


def _run_manager(
    engine: Engine,
    deps: ManagerDeps,
    now: datetime | None,
    shadow: bool,
    halt_file: Path | None,
    workers: int,
) -> ManagerResult:
    if not shadow and deps.execute is None:
        # Not a mode check — `argus.config` already refuses MODE=auto|approve without
        # LIVE_TRADING=I_UNDERSTAND. This is the second lock: a live run that was never given a
        # way to place orders would otherwise research, approve, and quietly place nothing.
        raise NotImplementedError("a live run needs an executor in ManagerDeps.execute")
    settings = get_settings()
    # The decision profile, once per run and from the venue alone (DECISION_LOGIC §11).
    limits = settings.limits
    moment = now or datetime.now(UTC)
    halt_path = halt_file or settings.halt_file
    run_id = new_id()
    result = ManagerResult(run_id=run_id, shadow=shadow)
    started = time.monotonic()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r, 'manager', :m, :t, 'RUNNING', :shadow, :venue)"
            ),
            # `shadow` was hardcoded to 1, so every run — including an armed one that could place
            # orders — was journalled as having placed nothing (F-125). The audit trail has to
            # record what the run was ALLOWED to do, not what we hoped it would do.
            {
                "r": run_id,
                "m": settings.mode,
                "t": moment.isoformat(),
                "shadow": int(shadow),
                "venue": settings.venue,
            },
        )

    def finish(status: str, error: str | None = None) -> ManagerResult:
        elapsed = time.monotonic() - started
        metrics.observe(metrics.RUN_DURATION, elapsed, unit="s", kind="manager", status=status)
        failed = None
        if status == "FAILED":
            # J-N4: a failed run alerted nobody. The row says FAILED; this says it to a human.
            failed = alerts.Alert(
                "manager_failed", "critical", f"manager run {run_id[:8]} failed: {error or '?'}"
            )
        alerts.dispatch(
            [alerts.run_over_budget("manager", elapsed, settings.run_budget_seconds), failed]
        )
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE runs SET status=:s, finished_at=:t, error=:e WHERE run_id=:r"),
                {"s": status, "t": utc_now(), "e": error, "r": run_id},
            )
        result.status = status
        return result

    if halt_path.exists():
        result.halted = True
        log.warning("manager.halted", halt=str(halt_path))
        return finish("HALTED", halt_path.read_text(encoding="utf-8").strip()[:500])

    try:
        with spans.span("manager.setup", run_id=run_id):
            assets = deps.assets()
            view, _ = deps.regime(assets)
            result.regime_label = view.label
            _record_regime(engine, run_id, view, moment)
            equity, free_margin, positions = deps.portfolio()
    except Exception as exc:  # noqa: BLE001 - a failed run must still close its row
        log.error("manager.failed", error=str(exc))
        result.errors.append(f"{type(exc).__name__}: {exc}")
        return finish("FAILED", f"{type(exc).__name__}: {exc}")

    # Everything after setup runs inside one handler (review J-N4): an exception here used to
    # leave the run row RUNNING for ever and exit through a traceback, which is the one outcome
    # nothing downstream — staleness alert, dashboard, the next run — can read.
    try:
        # Built before any research: a run that cannot read its own book must not spend a model
        # call, let alone place an order. Any failure here closes the run FAILED (fail closed).
        history = trading_history(engine, equity, moment, limits)
        book = gate_book(engine, list(positions))
        peak = peak_equity_30d(engine, equity, moment)
        miss_rate = downside_miss_rate(engine)
        # §9 "margin ratio < 200 % on any position": the lowest the venue reports.
        # One position the venue gave no ratio for makes the book's ratio unknown (gate 11 then
        # blocks entries and says so); min() over the rest would hide it.
        known = [p.margin_ratio for p in positions if p.margin_ratio is not None]
        margin_ratio = min(known, default=None) if len(known) == len(positions) else None
        switches = active_kill_switches(
            engine,
            list(positions),
            deps.last_close,
            view.inputs.real_yield_10y if view.inputs else None,
        )
        profile, steps, max_positions = risk_policy(engine, equity, moment)
        log.info(
            "manager.risk_profile",
            profile=profile,
            steps=[s.step for s in steps],
            decision_limits=limits.name,
        )

        # Research every asset at once (F-059), then journal in universe order on this thread:
        # SQLite has a single writer, and a stable order keeps runs comparable.
        with spans.span("manager.research", run_id=run_id, assets=len(assets), workers=workers):
            researched = run_parallel(
                {asset.symbol: partial(_research, deps, run_id, asset, view) for asset in assets},
                workers=workers,
            )

        # One call over everything that survived research. Doing it here rather than inside the
        # parallel block is the point of the change: the Trader sees the whole candidate set, ranks
        # it, and returns at most two plans for the cycle.
        batched: dict[str, PlanInput] = {}
        if deps.propose_batch is not None:
            candidates = [
                (asset, outcome[0], outcome[1])
                for asset in assets
                if not isinstance(outcome := researched.get(asset.symbol), BaseException)
                and outcome is not None
                and outcome[1] is not None
            ]
            if candidates:
                with spans.span("manager.propose", run_id=run_id, candidates=len(candidates)):
                    try:
                        batched = deps.propose_batch(run_id, candidates)
                    except Exception as exc:  # noqa: BLE001 - no plans is a result, not a dead run
                        result.errors.append(f"propose: {type(exc).__name__}: {exc}")
                        log.error("manager.propose_failed", error=str(exc))

        for asset in assets:
            outcome = researched.get(asset.symbol)
            if isinstance(outcome, InstrumentNotListedError):
                result.not_listed.append(asset.symbol)
                log.info("manager.not_listed", symbol=asset.symbol, detail=str(outcome))
                continue
            if isinstance(outcome, BaseException):
                result.errors.append(f"{asset.symbol}: {type(outcome).__name__}: {outcome}")
                log.warning("manager.research_failed", symbol=asset.symbol, error=str(outcome))
                continue
            if outcome is None:
                continue
            dossier, market, plan = outcome
            if deps.propose_batch is not None:
                # The batch answer is keyed by symbol; an asset the Trader did not pick simply has
                # no plan, the same "no trade" the per-asset path expresses by returning None.
                plan = batched.get(asset.symbol)
            dossier_id = _record_dossier(engine, run_id, dossier, moment)
            result.dossiers += 1

            if not dossier.trade_critical_present:
                result.skipped_for_gaps += 1
                continue
            if plan is None or market is None:
                continue  # NO_TRADE is a first-class outcome (F-001)

            portfolio = PortfolioState(
                equity=equity,
                free_margin=free_margin,
                positions=list(book),
                history=history,
                peak_equity_30d=peak,
                margin_ratio=margin_ratio,
                downside_miss_rate=miss_rate,
                active_kill_switches=switches,
                venue_positions=len(positions),
            )
            try:
                if plan.symbol.upper() != asset.symbol.upper():
                    # L-9: the per-asset path never checked this. A plan for another symbol was
                    # gated on THIS asset's price and handed THIS asset's instrument id.
                    raise ValueError(
                        f"plan is for {plan.symbol} but was researched as {asset.symbol}"
                    )
                if deps.quote is not None:
                    # Inside the handler: a quote that cannot be read is a REJECT for this plan,
                    # and the journalled market is the one the gate actually judged.
                    fresh = deps.quote(asset)
                    # Consensus travels with the mark it checked (D-2): a research-time "agreed"
                    # says nothing about a price read minutes later.
                    market = dataclasses.replace(
                        market,
                        mark=fresh.mark,
                        spread_pct=fresh.spread_pct,
                        consensus_ok=fresh.consensus_ok,
                    )
                # Demo only (owner 2026-10-26): targets inside the R:R floor are moved out to it
                # rather than rejected; stop, entry and size are untouched. Live never repairs.
                # Demo only (owner 2026-10-29): a stop wider than the ATR band moves in to its edge
                # if that is still beyond structure. First, so targets are measured in the new R.
                plan, stop_note = repair_stop(plan, limits, market)
                if stop_note:
                    log.info("manager.stop_repaired", symbol=asset.symbol, detail=stop_note)
                plan, repair_note = repair_targets(plan, limits, mark=market.mark)
                if repair_note:
                    log.info("manager.targets_repaired", symbol=asset.symbol, detail=repair_note)
                gate = evaluate(
                    plan,
                    market,
                    portfolio,
                    steps,
                    moment,
                    max_positions=max_positions,
                    limits=limits,
                )
            except Exception as exc:  # noqa: BLE001 - an undecidable plan is a REJECT, not a dead run
                # G-N4 / G-19: this call sat outside any handler, so one bad input (mark=0, a NaN
                # price) left the run RUNNING and every later asset ungated. Journalled like any
                # other verdict, because a gate that broke is exactly what a reviewer needs to see.
                log.error("manager.gate_error", symbol=asset.symbol, error=str(exc))
                result.errors.append(f"{asset.symbol}: gate error: {type(exc).__name__}: {exc}")
                gate = gate_error(exc)
            plan_id = _record_decision(
                engine, run_id, plan, gate, dossier_id, settings.mode, market
            )
            result.plans += 1
            if gate.halt and shadow:
                # A shadow run may be gating an invented `--equity` against the real journal, so
                # its loss percentages are not the account's. It reports; it never stops the
                # armed runs that share the HALT file.
                why = "; ".join(r.detail for r in gate.reasons if r.verdict == "REJECT")
                log.warning("manager.halt_not_written_in_shadow", reason=why)
            elif gate.halt and not halt_path.exists():
                # Gate 9 demands a HALT (weekly loss limit). Written, not just reported: it stays
                # until a human clears it, rather than lifting when the week rolls (G-13c).
                why = "; ".join(r.detail for r in gate.reasons if r.verdict == "REJECT")
                write_halt(halt_path, f"manager: {why}")
                log.error("manager.halt_written", halt=str(halt_path), reason=why)
                alerts.dispatch([alerts.Alert("manager_halt", "critical", f"HALT written: {why}")])
            if gate.status in PLACEABLE and gate.sized is not None:
                # Counted at the verdict, shadow or not and whatever the venue says next: caps
                # count attempts (F-029), and the next plan in this run must see this one's risk,
                # margin and symbol exactly as it will see a resting order next run.
                history = dataclasses.replace(
                    history,
                    entries_this_run=history.entries_this_run + 1,
                    entries_today=history.entries_today + 1,
                )
                book.append(
                    OpenPosition(
                        plan.symbol.upper(),
                        plan.side,
                        gate.sized.notional_usd,
                        gate.sized.risk_usd,
                        market.beta_btc,
                    )
                )
                free_margin -= gate.sized.margin_usd
            if gate.status == "APPROVED":
                result.approved += 1
            elif gate.status == "ADJUSTED":
                result.adjusted += 1
            else:
                result.rejected += 1
                for reason in gate.reasons:
                    if reason.verdict == "REJECT":
                        metrics.count(
                            metrics.GATE_REJECTIONS, symbol=asset.symbol, reason=reason.name
                        )

            # Placement is the last thing that happens to a plan, after it is journalled. A failure
            # here costs this one trade and nothing else: the loop continues, the error is
            # recorded, and the next asset is judged on its own merits. One venue refusal must not
            # silently end a run that still has fourteen assets to look at.
            # `not shadow` first and explicitly. Shadow is the stronger claim — it means nothing
            # reaches a venue whatever else is true — so it must not depend on an executor happening
            # to be absent. A shadow run handed an executor still places nothing.
            if not shadow and deps.execute is not None and gate.status in PLACEABLE:
                if halt_path.exists():
                    # HALT is read again before every placement, not only at the start: research
                    # is minutes of model calls, and `argus halt` during them must still stop the
                    # orders (review 2026-10-07, second pass). The plan stays journalled.
                    result.halted = True
                    result.errors.append(
                        f"{asset.symbol}: not placed — HALT appeared during the run"
                    )
                    log.warning("manager.halted_mid_run", symbol=asset.symbol, halt=str(halt_path))
                    continue
                with spans.span("manager.place", run_id=run_id, symbol=asset.symbol):
                    try:
                        if deps.execute(run_id, plan, gate, plan_id, asset.ccxt):
                            result.orders_placed += 1
                    except Exception as exc:  # noqa: BLE001 - one refusal is not a dead run
                        result.errors.append(f"{asset.symbol}: place failed: {exc}")
                        log.error("manager.place_failed", symbol=asset.symbol, error=str(exc))

    except Exception as exc:  # noqa: BLE001 - a failed run must still close its row
        log.error("manager.failed", error=str(exc))
        result.errors.append(f"{type(exc).__name__}: {exc}")
        return finish("FAILED", f"{type(exc).__name__}: {exc}"[:500])
    except BaseException as exc:
        # SIGTERM from systemd, Ctrl-C: close the row, then let the interrupt carry on.
        finish("FAILED", f"interrupted: {type(exc).__name__}")
        raise

    log.info("manager.done", **{k: v for k, v in vars(result).items() if k != "errors"})
    if result.halted:
        return finish("HALTED", "HALT appeared during the run; nothing placed after it")
    return finish("OK")
