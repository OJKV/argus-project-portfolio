"""Muse pre-live review (2026-10): P0-1, P0-2, P1-1, P1-4 and the fees interval guard.

Each test names the finding it reproduces. The common thread: a missing or impossible input must
close the gate, never open it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from test_rules import _gates, _plan, _portfolio, _run
from test_sizing import _inp

from argus.gate.fees import FeeRates, expected_costs
from argus.gate.killswitch import KillRule, Triggered, conflicts_with_plan, portfolio_kill
from argus.gate.protections import TradingHistory, check_protections
from argus.gate.rules import OpenPosition
from argus.gate.sizing import size_position

_EVERY_KIND = [
    KillRule("price_close_above", level=105.0),
    KillRule("price_close_below", level=95.0),
    KillRule("funding_flip", sign="negative"),
    KillRule("event", event="CPI", outcome="hot"),
    KillRule("real_yield_above", level=2.0),
]


@pytest.mark.parametrize("rule", _EVERY_KIND, ids=lambda r: r.kind)
@pytest.mark.parametrize("held", ["long", "short"])
def test_p0_1_every_fired_rule_blocks_entries_on_its_positions_side(
    rule: KillRule, held: str
) -> None:
    fired = [Triggered(rule, "fired", held)]
    other = "short" if held == "long" else "long"
    assert conflicts_with_plan(held, fired)
    assert not conflicts_with_plan(other, fired)


def test_p0_1_a_trigger_with_no_known_side_blocks_both_sides() -> None:
    fired = [Triggered(KillRule("funding_flip", sign="negative"), "fired")]
    assert conflicts_with_plan("long", fired) and conflicts_with_plan("short", fired)


def test_p0_1_gate_11_blocks_a_funding_flip_on_the_open_positions_side_only() -> None:
    fired = [Triggered(KillRule("funding_flip", sign="negative"), "fired", "long")]
    assert _run(portfolio=_portfolio(active_kill_switches=fired)).status == "REJECTED"
    short = _plan(
        side="short",
        stop=106.0,
        invalidation_rules=[{"kind": "price_close_above", "level": 106}],
    )
    result = _run(short, portfolio=_portfolio(active_kill_switches=fired))
    assert _gates(result).get(11) != "REJECT"


def test_p0_2_unknown_margin_with_open_positions_blocks_entries_only() -> None:
    kill = portfolio_kill(1000.0, 1000.0, None, has_open_positions=True)
    assert kill.block_entries and kill.reasons
    assert not (kill.close_all or kill.halt or kill.reduce)


def test_p0_2_unknown_margin_with_no_positions_is_nothing_to_measure() -> None:
    kill = portfolio_kill(1000.0, 1000.0, None, has_open_positions=False)
    assert not kill.block_entries and not kill.reasons


@pytest.mark.parametrize("peak", [0.0, -5.0, float("nan")])
def test_p0_2_an_unusable_30d_high_is_explicit_not_silent(peak: float) -> None:
    kill = portfolio_kill(1000.0, peak, 900.0, has_open_positions=False)
    assert kill.block_entries and kill.reasons
    assert not (kill.close_all or kill.halt)


def test_p0_2_gate_11_refuses_entries_when_margin_is_unknown_with_open_positions() -> None:
    held = OpenPosition("ETH", "long", 300.0, 10.0, 1.0)
    result = _run(portfolio=_portfolio(positions=[held], margin_ratio=None))
    assert result.status == "REJECTED" and _gates(result)[11] == "REJECT"
    assert "margin ratio unknown" in result.reasons[-1].detail
    assert not result.halt


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), -0.1, 1.5])
def test_p1_1_a_bad_downside_miss_rate_rejects_instead_of_skipping_gate_14(rate: float) -> None:
    result = _run(portfolio=_portfolio(downside_miss_rate=rate))
    assert result.status == "REJECTED" and _gates(result)[14] == "REJECT"


@pytest.mark.parametrize("atr", [float("nan"), float("inf"), -1.0, 0.0])
def test_p1_1_a_bad_atr_fails_closed_instead_of_skipping_the_band(atr: float) -> None:
    assert not size_position(_inp(2000.0, atr_pct=atr)).ok


@pytest.mark.parametrize("equity", [0.0, -50.0, float("nan")])
def test_p1_4_non_positive_equity_never_passes_as_zero_percent_heat(equity: float) -> None:
    result = _run(portfolio=_portfolio(equity=equity, peak_equity_30d=2000.0))
    assert result.status == "REJECTED"


def _check(**over: Any) -> Any:
    base: dict[str, Any] = {
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "consecutive_losses": 0,
        "entries_this_run": 0,
        "entries_today": 0,
        "stop_outs_by_symbol": {},
        "last_loss_at": None,
        "trades_since_pause": 99,
    }
    base.update(over)
    return check_protections("BTC", TradingHistory(**base), datetime.now(UTC))


@pytest.mark.parametrize(
    "over",
    [
        {"entries_today": -1},
        {"consecutive_losses": -3},
        {"entries_this_run": -1},
        {"trades_since_pause": -1},
        {"daily_pnl_pct": None},
        {"weekly_pnl_pct": float("nan")},
        {"daily_pnl_pct": float("inf")},
    ],
)
def test_p1_4_impossible_history_values_reject_with_a_reason(over: dict[str, Any]) -> None:
    state = _check(**over)
    assert not state.allowed and "history invalid" in state.reasons[0]


def test_p1_4_a_sane_history_still_passes() -> None:
    assert _check().allowed


@pytest.mark.parametrize("interval", [0.0, -8.0, float("nan")])
def test_fees_a_non_positive_funding_interval_is_refused(interval: float) -> None:
    rates = FeeRates(Decimal("-0.0002"), Decimal("-0.0005"))
    with pytest.raises(ValueError, match="interval_hours"):
        expected_costs(1000.0, rates, True, 0.0001, 24.0, interval_hours=interval)


def test_p0_2_planned_entries_alone_are_not_an_unknown_venue_margin() -> None:
    planned = OpenPosition("ETH", "long", 300.0, 10.0, 1.0)
    result = _run(portfolio=_portfolio(positions=[planned], margin_ratio=None, venue_positions=0))
    assert _gates(result).get(11) != "REJECT"
