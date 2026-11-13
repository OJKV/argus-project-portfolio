"""The PnL panel: dollars and percent from journal rows, per account, AI apart from manual.

A closed trade is a CLOSED `positions_snapshots` row; its dollars are the `realised_pnl` the
watchdog wrote on it. The percentages are over the latest equity snapshot (what the sizing gate
sizes from), and a figure that cannot be computed is None, never zero.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.journal import positions
from argus.journal.db import new_id, open_journal
from argus.ui import pnl

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.fixture
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from argus.ui import app as app_module

    monkeypatch.setattr(app_module, "open_journal_readonly", lambda *a, **k: engine)
    return TestClient(app_module.create_app())


def _run(engine: Engine, venue: str, kind: str, at: datetime) -> str:
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r, :k, 'demo', :t, 'OK', 0, :v)"
            ),
            {"r": run_id, "k": kind, "t": at.isoformat(), "v": venue},
        )
    return run_id


def _equity(engine: Engine, venue: str, at: datetime, equity: str) -> None:
    run_id = _run(engine, venue, "watchdog", at)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO equity_snapshots (snapshot_id, run_id, created_at, equity)"
                " VALUES (:i, :r, :t, :e)"
            ),
            {"i": new_id(), "r": run_id, "t": at.isoformat(), "e": equity},
        )


def _trade(
    engine: Engine,
    venue: str,
    asset: str,
    *,
    closed: datetime,
    realised: str | None,
    kind: str = "manager",
    risk: float | None = 10.0,
    has_plan: bool = True,
    side: str = "long",
    fee: str | None = None,
) -> None:
    """One closed trade: a plan and entry order, an open snapshot, then the CLOSED row."""
    symbol = f"{asset}/USDT:USDT"
    opened = closed - timedelta(hours=6)
    run_id = _run(engine, venue, kind, opened)
    if has_plan:
        plan_id = new_id()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO plans (plan_id, run_id, created_at, mode, symbol, side, state,"
                    " payload, prev_hash, row_hash) VALUES (:p, :r, :t, 'demo', :s, :side,"
                    " 'PLACED', '{}', '', '')"
                ),
                {"p": plan_id, "r": run_id, "t": opened.isoformat(), "s": asset, "side": side},
            )
            conn.execute(
                text(
                    "INSERT INTO gate_results (gate_result_id, plan_id, run_id, created_at,"
                    " verdict, reasons, prev_hash, row_hash) VALUES (:g, :p, :r, :t,"
                    " 'APPROVED', :j, '', '')"
                ),
                {
                    "g": new_id(),
                    "p": plan_id,
                    "r": run_id,
                    "t": opened.isoformat(),
                    "j": json.dumps({"sized": {"risk_usd": risk}}),
                },
            )
            order_id = new_id()
            conn.execute(
                text(
                    "INSERT INTO orders (order_id, cl_ord_id, run_id, plan_id, created_at,"
                    " symbol, side, kind, order_type, price, size, state, raw_request,"
                    " prev_hash, row_hash) VALUES (:o, :c, :r, :p, :t, :s, 'buy', 'entry',"
                    " 'market', '100', '1', 'NEW', '{}', '', '')"
                ),
                {
                    "o": order_id,
                    "c": f"cl-{asset}",
                    "r": run_id,
                    "p": plan_id,
                    "t": opened.isoformat(),
                    "s": symbol,
                },
            )
            if fee is not None:
                conn.execute(
                    text(
                        "INSERT INTO fills (fill_id, order_id, run_id, created_at, symbol, side,"
                        " price, size, fee, fee_ccy, raw, prev_hash, row_hash) VALUES (:f, :o,"
                        " :r, :t, :s, 'buy', '100', '1', :fee, 'USDT', '{}', '', '')"
                    ),
                    {
                        "f": new_id(),
                        "o": order_id,
                        "r": run_id,
                        "t": opened.isoformat(),
                        "s": symbol,
                        "fee": fee,
                    },
                )
    watch = _run(engine, venue, "watchdog", closed - timedelta(minutes=20))
    positions.record(
        engine,
        watch,
        [
            {
                "symbol": symbol,
                "side": side,
                "size": "2",
                "entry_price": "100",
                "mark_price": "101",
                "leverage": "2",
                "margin": "100",
            }
        ],
        now=closed - timedelta(minutes=20),
    )
    last_seen = positions.latest_open(engine, venue=venue)[symbol]
    marker = _run(engine, venue, "watchdog", closed)
    positions.mark_closed(
        engine,
        marker,
        [positions.Closed(symbol=symbol, last_seen=last_seen)],
        now=closed,
        realised=None if realised is None else {symbol: Decimal(realised)},
    )


def _book(engine: Engine) -> None:
    """Demo: four priced closes (three AI, one manual), one unpriced, equity $1 000. Live: one
    large win that must never reach the demo page."""
    _equity(engine, "demo", NOW - timedelta(days=45), "900")
    _equity(engine, "demo", NOW - timedelta(hours=1), "1000")
    _trade(engine, "demo", "AAA", closed=NOW - timedelta(hours=2), realised="30", fee="-0.5")
    _trade(engine, "demo", "BBB", closed=NOW - timedelta(days=3), realised="-10")
    _trade(engine, "demo", "CCC", closed=NOW - timedelta(days=20), realised="5", kind="manual")
    _trade(engine, "demo", "DDD", closed=NOW - timedelta(days=40), realised="20")
    _trade(engine, "demo", "EEE", closed=NOW - timedelta(days=2), realised=None)
    _equity(engine, "live", NOW - timedelta(hours=1), "250")
    _trade(engine, "live", "LLL", closed=NOW - timedelta(hours=1), realised="1000")


def _by_period(windows: list[pnl.Window]) -> dict[str, pnl.Window]:
    return {w.period: w for w in windows}


def test_realised_dollars_and_percent_per_window(engine: Engine) -> None:
    _book(engine)
    panel = pnl.pnl_panel(engine, "demo", now=NOW)
    w = _by_period(panel.windows["all"])
    # Close time, UTC: "today" starts 00:00, 7d and 30d roll back from now.
    assert (w["today"].closed, w["today"].net_pnl_usd) == (1, 30.0)
    assert (w["7d"].closed, w["7d"].priced, w["7d"].net_pnl_usd) == (
        3,
        2,
        20.0,
    )  # +30 -10, 1 unpriced
    assert (w["30d"].closed, w["30d"].net_pnl_usd) == (4, 25.0)
    assert (w["all"].closed, w["all"].net_pnl_usd) == (5, 45.0)
    # Percent of the latest equity ($1 000), the figure the sizing gate sizes from.
    assert panel.basis_usd == 1000.0
    assert w["today"].net_pnl_pct == pytest.approx(3.0)
    assert w["all"].net_pnl_pct == pytest.approx(4.5)


def test_win_rate_profit_factor_and_unpriced_closes_are_not_zeros(engine: Engine) -> None:
    _book(engine)
    s = pnl.pnl_panel(engine, "demo", now=NOW).summary
    assert (s.closed, s.unpriced, s.wins, s.losses) == (5, 1, 3, 1)
    assert s.win_rate == pytest.approx(0.75)  # of the four that have a booked figure
    assert s.profit_factor == pytest.approx(55 / 10)
    # R is the dollars made over the $10 the gate approved risking: (3 - 1 + 0.5 + 2) / 4.
    assert s.avg_r == pytest.approx(1.12, abs=0.01)


def test_ai_and_manual_trades_are_kept_apart(engine: Engine) -> None:
    _book(engine)
    panel = pnl.pnl_panel(engine, "demo", now=NOW)
    ai, manual = _by_period(panel.windows["ai"]), _by_period(panel.windows["manual"])
    assert ai["all"].net_pnl_usd == 40.0 and ai["all"].closed == 4
    assert manual["all"].net_pnl_usd == 5.0 and manual["all"].closed == 1
    assert manual["7d"].closed == 0 and manual["7d"].net_pnl_usd == 0.0
    assert manual["7d"].win_rate is None  # no trades is not a 0% win rate
    origins = {t.symbol.split("/")[0]: t.origin for t in panel.trades}
    assert origins["CCC"] == "manual" and origins["AAA"] == "ai"


def test_a_close_with_no_plan_behind_it_is_origin_unknown(engine: Engine) -> None:
    _equity(engine, "demo", NOW - timedelta(hours=1), "1000")
    _trade(engine, "demo", "ZZZ", closed=NOW - timedelta(hours=2), realised="7", has_plan=False)
    panel = pnl.pnl_panel(engine, "demo", now=NOW)
    assert [t.origin for t in panel.trades] == ["unknown"]
    assert panel.windows["ai"][3].closed == 0 and panel.windows["unknown"][3].closed == 1


def test_the_account_filter_never_mixes_demo_and_live(engine: Engine) -> None:
    _book(engine)
    live = pnl.pnl_panel(engine, "live", now=NOW)
    assert [t.symbol for t in live.trades] == ["LLL/USDT:USDT"]
    assert _by_period(live.windows["all"])["all"].net_pnl_usd == 1000.0
    assert live.basis_usd == 250.0  # not the demo account's $1 000
    assert _by_period(live.windows["all"])["all"].net_pnl_pct == pytest.approx(400.0)
    demo = pnl.pnl_panel(engine, "demo", now=NOW)
    assert "LLL/USDT:USDT" not in [t.symbol for t in demo.trades]


def test_a_run_with_no_recorded_account_is_attributed_to_neither(engine: Engine) -> None:
    _trade(engine, "-", "OLD", closed=NOW - timedelta(hours=2), realised="99")
    for venue in ("demo", "live"):
        assert pnl.pnl_panel(engine, venue, now=NOW).trades == []


def test_per_trade_percent_says_what_it_is_of(engine: Engine) -> None:
    _equity(engine, "demo", NOW - timedelta(days=1), "500")  # equity before the entry
    _trade(engine, "demo", "AAA", closed=NOW - timedelta(hours=2), realised="25")
    _trade(engine, "demo", "BBB", closed=NOW - timedelta(days=5), realised="5")
    by = {t.symbol.split("/")[0]: t for t in pnl.pnl_panel(engine, "demo", now=NOW).trades}
    assert by["AAA"].pct_basis == "equity at entry" and by["AAA"].pnl_pct == pytest.approx(5.0)
    # BBB's entry predates every equity reading: margin 100 x leverage 2 = 200 notional.
    assert by["BBB"].pct_basis == "notional" and by["BBB"].pnl_pct == pytest.approx(2.5)


def test_fees_come_from_journalled_fills_and_are_a_dash_without_them(engine: Engine) -> None:
    _book(engine)
    panel = pnl.pnl_panel(engine, "demo", now=NOW)
    by = {t.symbol.split("/")[0]: t for t in panel.trades}
    assert by["AAA"].fees_usd == pytest.approx(0.5)  # OKX reports a charge as -0.5
    assert by["BBB"].fees_usd is None
    assert panel.summary.fees_usd == pytest.approx(0.5)


def test_unrealised_is_the_open_positions_not_a_guess(engine: Engine) -> None:
    _equity(engine, "demo", NOW - timedelta(hours=1), "1000")
    run_id = _run(engine, "demo", "watchdog", NOW)
    positions.record(
        engine,
        run_id,
        [{"symbol": "OPN/USDT:USDT", "side": "long", "size": "1", "unrealised": "12.5"}],
        now=NOW,
    )
    assert pnl.pnl_panel(engine, "demo", now=NOW).summary.unrealised_usd == 12.5
    flat = pnl.pnl_panel(engine, "live", now=NOW).summary
    assert (flat.open_positions, flat.unrealised_usd) == (0, 0.0)


def test_max_drawdown_is_peak_to_trough() -> None:
    day = datetime(2026, 10, 1, tzinfo=UTC)
    series = [(day + timedelta(days=i), v) for i, v in enumerate([100.0, 120.0, 90.0, 110.0])]
    assert pnl.max_drawdown_pct(series) == 25.0
    assert pnl.max_drawdown_pct(series[:1]) is None


def test_sharpe_is_a_dash_until_there_are_fourteen_daily_points() -> None:
    day = datetime(2026, 10, 1, 12, tzinfo=UTC)
    short = [(day + timedelta(days=i), 100.0 + i % 2) for i in range(13)]
    assert pnl.sharpe_daily(short) == (None, 13)
    two_trades = [(day, 100.0), (day + timedelta(days=1), 130.0)]
    assert pnl.sharpe_daily(two_trades)[0] is None


def test_sharpe_uses_the_last_reading_of_each_utc_day_annualised_by_root_365() -> None:
    day = datetime(2026, 10, 1, tzinfo=UTC)
    equities = [100.0, 101.0, 100.0, 102.0, 101.0, 103.0, 102.0, 104.0, 103.0, 105.0]
    equities += [104.0, 106.0, 105.0, 107.0, 106.0]  # 15 daily closes
    series: list[tuple[datetime, float]] = []
    for i, value in enumerate(equities):
        series.append((day + timedelta(days=i, hours=1), value - 50.0))  # an early, stale read
        series.append((day + timedelta(days=i, hours=23), value))
    got, days = pnl.sharpe_daily(series)
    returns = [(b - a) / a for a, b in zip(equities, equities[1:], strict=False)]
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    assert days == 15
    assert got == pytest.approx(mean / sd * math.sqrt(365))


def test_a_flat_equity_line_has_no_sharpe_rather_than_infinity() -> None:
    day = datetime(2026, 10, 1, tzinfo=UTC)
    assert pnl.sharpe_daily([(day + timedelta(days=i), 100.0) for i in range(20)])[0] is None


def test_no_equity_means_no_percent_not_zero_percent(engine: Engine) -> None:
    _trade(engine, "demo", "AAA", closed=NOW - timedelta(hours=2), realised="30")
    panel = pnl.pnl_panel(engine, "demo", now=NOW)
    assert panel.basis_usd is None
    today = _by_period(panel.windows["all"])["today"]
    assert today.net_pnl_usd == 30.0 and today.net_pnl_pct is None


def test_an_empty_journal_is_an_empty_panel(engine: Engine) -> None:
    panel = pnl.pnl_panel(engine, "demo", now=NOW)
    assert panel.trades == [] and panel.equity_curve is None and panel.pnl_curve is None
    s = panel.summary
    assert (s.closed, s.win_rate, s.profit_factor, s.avg_r, s.sharpe) == (0, None, None, None, None)
    for w in panel.windows["all"]:
        assert (w.closed, w.net_pnl_usd, w.net_pnl_pct, w.win_rate) == (0, 0.0, None, None)


def test_the_page_renders_for_each_account_and_when_empty(
    client: TestClient, engine: Engine
) -> None:
    empty = client.get("/pnl?venue=demo")
    assert empty.status_code == 200
    assert "No closed trades on the demo account yet." in empty.text

    _equity(engine, "demo", datetime.now(UTC) - timedelta(hours=1), "1000")
    _trade(engine, "demo", "AAA", closed=datetime.now(UTC) - timedelta(hours=2), realised="30.5")
    _trade(engine, "live", "LLL", closed=datetime.now(UTC) - timedelta(hours=2), realised="777")
    demo = client.get("/pnl?venue=demo")
    words = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", demo.text))
    assert "AAA/USDT:USDT" in words and "LLL/USDT:USDT" not in words
    assert "+$30.50" in words and "777" not in words
    assert "Sharpe (daily, annualised, rf=0)" in words and "not meaningful yet" in words
    assert "latest equity" in words  # says what the percent is of
    assert "no open positions" in words and "unreadable" not in words
    assert "LLL/USDT:USDT" in client.get("/pnl?venue=live").text
