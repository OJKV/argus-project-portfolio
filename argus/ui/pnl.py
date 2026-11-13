"""The PnL panel: what ARGUS actually made or lost on one account, from rows already in the journal.

Read-only and built only from the journal — no venue call. A closed trade is a CLOSED row in
`positions_snapshots` (the only close signal the system has, F-132), and its dollars are the
`realised_pnl` the watchdog wrote on that row when it noticed the close: the venue's own booked
figure, which on OKX is price PnL net of fees and funding. A close the venue could not price has
no `realised_pnl`; it is counted and named, never booked as zero and never estimated from the plan
(the manager's loss limits may fall back to the plan stop; a performance page may not).

What the percentages are of
---------------------------
Window percentages (today / 7d / 30d / all) are net PnL over the account's LATEST equity snapshot:
that is what the sizing gate sizes every trade from (`SizingInput.equity_usd` is the portfolio's
equity), using the same basis for each window (`pct_of`). Per-trade
percentages are over the equity snapshot at entry; with none before the entry, over notional
(margin x leverage) — each row says which. No basis gives None, shown as a dash, not 0.

Origin: a plan written by a `manual` run (`argus manual-trade`) is MANUAL; any other plan is AI; a
close with no plan behind it is UNKNOWN, because nothing journalled says who opened it.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import Engine, text

from argus.ui import queries

Origin = Literal["ai", "manual", "unknown"]
ORIGINS: tuple[str, ...] = ("all", "ai", "manual", "unknown")
PERIODS = ("today", "7d", "30d", "all")

# Fewer daily equity points than this and a Sharpe ratio is noise: two lucky days would print
# an enormous number. Shown as a dash with a note until the history is long enough.
SHARPE_MIN_DAYS = 14
# Crypto trades every day, so a year is 365 daily returns, not 252.
SHARPE_PERIODS_PER_YEAR = 365
TABLE_ROWS = 100
CURVE_POINTS = 300


class Trade(BaseModel):
    symbol: str
    side: str
    origin: Origin
    opened_at: str | None  # when the entry order was journalled; None with no plan behind it
    closed_at: str  # when someone NOTICED the close, up to a watchdog tick after it happened
    size: float | None
    entry: float | None
    last_mark: float | None  # last mark seen before it closed: approximates, is not, the exit
    pnl_usd: float | None  # the venue's booked figure; None = it could not say
    pnl_pct: float | None
    pct_basis: Literal["equity at entry", "notional"] | None
    r_multiple: float | None
    fees_usd: float | None  # None = no fills journalled for it


class Window(BaseModel):
    period: str
    closed: int
    priced: int  # of `closed`, how many carry a booked PnL; the rest are in no sum below
    wins: int
    win_rate: float | None
    net_pnl_usd: float
    net_pnl_pct: float | None
    fees_usd: float | None


class Curve(BaseModel):
    label: str
    points: str  # an SVG polyline `points` attribute
    first: float
    last: float
    low: float
    high: float
    n: int


class Summary(BaseModel):
    closed: int
    unpriced: int
    wins: int
    losses: int
    win_rate: float | None
    avg_r: float | None
    profit_factor: float | None
    fees_usd: float | None
    max_drawdown_pct: float | None
    sharpe: float | None
    sharpe_days: int
    open_positions: int
    unrealised_usd: float | None
    unrealised_as_of: str | None


class PnlPanel(BaseModel):
    venue: str
    basis_usd: float | None  # the equity every window percentage is over
    basis_as_of: str | None
    windows: dict[str, list[Window]]  # origin -> one Window per period
    summary: Summary
    trades: list[Trade]
    trades_total: int
    equity_curve: Curve | None
    pnl_curve: Curve | None


def _f(value: Any) -> float | None:
    try:
        out = None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None
    return out if out is not None and math.isfinite(out) else None


def _when(stamp: Any) -> datetime | None:
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return then if then.tzinfo else then.replace(tzinfo=UTC)


def _pct(value: float | None, basis: float | None) -> float | None:
    """`value` as a percentage of `basis`; None when there is no basis, never 0 or infinity."""
    if value is None or not basis or basis <= 0:
        return None
    return value / basis * 100.0


def _equity_series(engine: Engine, venue: str) -> list[tuple[datetime, float]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT e.created_at, e.equity FROM equity_snapshots e"
                " JOIN runs r ON r.run_id = e.run_id WHERE r.config_hash = :venue"
                " ORDER BY e.created_at"
            ),
            {"venue": venue},
        ).all()
    out: list[tuple[datetime, float]] = []
    for stamp, equity in rows:
        at, value = _when(stamp), _f(equity)
        if at is not None and value is not None:
            out.append((at, value))
    return out


def max_drawdown_pct(series: list[tuple[datetime, float]]) -> float | None:
    """Deepest peak-to-trough fall of the equity readings, in percent; None below two readings.

    It reads equity, so a deposit or withdrawal moves it like a trade would."""
    if len(series) < 2:
        return None
    peak, worst = series[0][1], 0.0
    for _, value in series:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return round(worst, 2)


def sharpe_daily(series: list[tuple[datetime, float]]) -> tuple[float | None, int]:
    """(annualised Sharpe, daily points used), risk-free rate zero.

    Daily return = change in the last equity reading of each UTC day over the day before's.
    None below SHARPE_MIN_DAYS daily points, or when the returns do not vary."""
    last_of_day: dict[Any, float] = {}
    for at, value in series:  # chronological, so the last write wins
        last_of_day[at.astimezone(UTC).date()] = value
    days = list(last_of_day.values())
    if len(days) < SHARPE_MIN_DAYS:
        return None, len(days)
    returns = [(b - a) / a for a, b in zip(days, days[1:], strict=False) if a > 0]
    if len(returns) < 2:
        return None, len(days)
    spread = statistics.stdev(returns)
    if spread == 0:
        return None, len(days)
    return statistics.mean(returns) / spread * math.sqrt(SHARPE_PERIODS_PER_YEAR), len(days)


def _curve(label: str, values: list[float]) -> Curve | None:
    """A 640x140 polyline of `values`, evenly spaced, thinned to CURVE_POINTS."""
    if len(values) < 2:
        return None
    n = len(values)
    if n > CURVE_POINTS:
        step = (n - 1) / (CURVE_POINTS - 1)
        values = [values[round(i * step)] for i in range(CURVE_POINTS)]
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    width, height, pad = 640.0, 140.0, 6.0
    xs = (width - 2 * pad) / (len(values) - 1)
    points = " ".join(
        f"{pad + i * xs:.1f},{height - pad - (v - low) / span * (height - 2 * pad):.1f}"
        for i, v in enumerate(values)
    )
    return Curve(
        label=label, points=points, first=values[0], last=values[-1], low=low, high=high, n=n
    )


def _scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _closed_rows(engine: Engine, venue: str) -> list[dict[str, Any]]:
    """Every close on `venue` with the open snapshot it closed from, oldest first, once each.

    Scoped by the run that wrote the CLOSED row, exactly as `positions.closed_since`; a close
    written by a run with no recorded account belongs to neither view. A close marked twice
    (two runs noticing it) is one trade, keyed on the snapshot it closed from."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT o.snapshot_id AS seen_id, o.created_at AS seen_at, o.symbol AS symbol,"
                    "       o.side AS side, o.size AS size, o.entry_price AS entry_price,"
                    "       o.mark_price AS mark_price, o.leverage AS leverage, o.margin AS margin,"
                    "       c.created_at AS closed_at,"
                    "       json_extract(c.raw, '$.realised_pnl') AS realised"
                    " FROM positions_snapshots c"
                    " JOIN positions_snapshots o"
                    "   ON o.snapshot_id = json_extract(c.raw, '$.closed_from')"
                    " JOIN runs r ON r.run_id = c.run_id"
                    " WHERE c.state = 'CLOSED' AND r.config_hash = :venue"
                    " ORDER BY c.created_at, c.rowid"
                ),
                {"venue": venue},
            )
            .mappings()
            .all()
        )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["seen_id"] not in seen:
            seen.add(row["seen_id"])
            out.append(dict(row))
    return out


def _trade(engine: Engine, venue: str, row: dict[str, Any]) -> Trade:
    from argus.journal.positions import Closed
    from argus.runs.review import plan_behind

    closed = Closed(
        symbol=row["symbol"],
        last_seen={
            "snapshot_id": row["seen_id"],
            "created_at": row["seen_at"],
            "symbol": row["symbol"],
            "side": row["side"],
            "entry_price": row["entry_price"],
        },
        closed_at=row["closed_at"],
    )
    placed = plan_behind(engine, closed, venue=venue)
    plan_id = placed.plan_id
    pnl = _f(row["realised"])

    origin: Origin = "unknown"
    risk: float | None = None
    outcome_r: float | None = None
    fees: float | None = None
    if plan_id is not None:
        kind = _scalar(
            engine,
            "SELECT r.kind FROM plans p JOIN runs r ON r.run_id = p.run_id WHERE p.plan_id = :p",
            p=plan_id,
        )
        origin = "manual" if kind == "manual" else "ai"
        gate = _scalar(
            engine,
            "SELECT reasons FROM gate_results WHERE plan_id = :p"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            p=plan_id,
        )
        try:
            risk = _f(((json.loads(gate) if gate else {}).get("sized") or {}).get("risk_usd"))
        except (TypeError, ValueError, AttributeError):
            risk = None
        outcome_r = _f(
            _scalar(engine, "SELECT outcome_r FROM postmortems WHERE plan_id = :p", p=plan_id)
        )
        charged = _scalar(
            engine,
            "SELECT SUM(CAST(f.fee AS REAL)) FROM fills f JOIN orders o ON o.order_id = f.order_id"
            " WHERE o.plan_id = :p",
            p=plan_id,
        )
        # OKX reports a charge as a negative number (F-036): "paid" is the sign-flipped sum.
        fees = None if charged is None else -float(charged)

    # R: the dollars the trade made over the dollars the gate approved risking. The post-mortem's
    # own R (entry/stop/last mark) is the fallback; with neither, no R.
    if pnl is not None and risk and risk > 0:
        r_multiple: float | None = pnl / risk
    else:
        r_multiple = outcome_r

    basis: float | None = None
    basis_label: Literal["equity at entry", "notional"] | None = None
    if placed.placed_at is not None:
        basis = _f(
            _scalar(
                engine,
                "SELECT e.equity FROM equity_snapshots e JOIN runs r ON r.run_id = e.run_id"
                " WHERE r.config_hash = :v AND e.created_at <= :t"
                " ORDER BY e.created_at DESC LIMIT 1",
                v=venue,
                t=placed.placed_at,
            )
        )
        basis_label = "equity at entry"
    if not basis or basis <= 0:
        margin, leverage = _f(row["margin"]), _f(row["leverage"])
        basis = margin * leverage if margin and leverage else None
        basis_label = "notional"
    return Trade(
        symbol=row["symbol"],
        side=str(row["side"]),
        origin=origin,
        opened_at=placed.placed_at,
        closed_at=row["closed_at"],
        size=_f(row["size"]),
        entry=_f(row["entry_price"]),
        last_mark=_f(row["mark_price"]),
        pnl_usd=pnl,
        pnl_pct=_pct(pnl, basis),
        pct_basis=basis_label if _pct(pnl, basis) is not None else None,
        r_multiple=None if r_multiple is None else round(r_multiple, 2),
        fees_usd=fees,
    )


def _window(period: str, trades: list[Trade], basis: float | None) -> Window:
    priced = [t.pnl_usd for t in trades if t.pnl_usd is not None]
    net = sum(priced)
    fees = [t.fees_usd for t in trades if t.fees_usd is not None]
    wins = sum(1 for p in priced if p > 0)
    return Window(
        period=period,
        closed=len(trades),
        priced=len(priced),
        wins=wins,
        win_rate=wins / len(priced) if priced else None,
        net_pnl_usd=round(net, 10),
        net_pnl_pct=_pct(net, basis) if priced else None,
        fees_usd=round(sum(fees), 10) if fees else None,
    )


def _windows(trades: list[Trade], basis: float | None, now: datetime) -> list[Window]:
    """today (since 00:00 UTC), rolling 7d, rolling 30d, all - by close time."""
    starts: dict[str, datetime | None] = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "all": None,
    }
    out: list[Window] = []
    for period, since in starts.items():
        chosen = [
            t
            for t in trades
            if since is None or ((at := _when(t.closed_at)) is not None and at >= since)
        ]
        out.append(_window(period, chosen, basis))
    return out


def pnl_panel(engine: Engine, venue: str, now: datetime | None = None) -> PnlPanel:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    trades = [_trade(engine, venue, row) for row in _closed_rows(engine, venue)]
    series = _equity_series(engine, venue)
    basis = series[-1][1] if series and series[-1][1] > 0 else None

    windows = {
        origin: _windows([t for t in trades if origin == "all" or t.origin == origin], basis, now)
        for origin in ORIGINS
    }

    priced = [t for t in trades if t.pnl_usd is not None]
    gains = sum(t.pnl_usd for t in priced if t.pnl_usd is not None and t.pnl_usd > 0)
    losses = -sum(t.pnl_usd for t in priced if t.pnl_usd is not None and t.pnl_usd < 0)
    wins = sum(1 for t in priced if t.pnl_usd is not None and t.pnl_usd > 0)
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    fees = [t.fees_usd for t in trades if t.fees_usd is not None]
    sharpe, sharpe_days = sharpe_daily(series)

    open_rows = queries.open_positions(engine, venue=venue)
    held = [_f(p["unrealised"]) for p in open_rows]
    summary = Summary(
        closed=len(trades),
        unpriced=len(trades) - len(priced),
        wins=wins,
        losses=sum(1 for t in priced if t.pnl_usd is not None and t.pnl_usd < 0),
        win_rate=wins / len(priced) if priced else None,
        avg_r=round(sum(rs) / len(rs), 2) if rs else None,
        # Undefined without a loss: None, never infinity.
        profit_factor=round(gains / losses, 2) if losses else None,
        fees_usd=round(sum(fees), 10) if fees else None,
        max_drawdown_pct=max_drawdown_pct(series),
        sharpe=None if sharpe is None else round(sharpe, 2),
        sharpe_days=sharpe_days,
        open_positions=len(open_rows),
        # Absent, not zero, when a position's own figure is unreadable; flat is a true zero.
        unrealised_usd=(
            None
            if any(x is None for x in held)
            else round(sum(x for x in held if x is not None), 10)
        ),
        unrealised_as_of=max((p["created_at"] for p in open_rows), default=None),
    )

    running = 0.0
    cumulative: list[float] = []
    for t in sorted(priced, key=lambda x: x.closed_at):
        running += t.pnl_usd or 0.0
        cumulative.append(running)
    return PnlPanel(
        venue=venue,
        basis_usd=basis,
        basis_as_of=series[-1][0].isoformat() if series else None,
        windows=windows,
        summary=summary,
        trades=sorted(trades, key=lambda t: t.closed_at, reverse=True)[:TABLE_ROWS],
        trades_total=len(trades),
        equity_curve=_curve("Equity", [v for _, v in series]),
        pnl_curve=_curve("Cumulative realised PnL", cumulative),
    )
