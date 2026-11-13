"""Kill-switches — DECISION_LOGIC.md §9, gate 11. Evaluated on closed candles only."""

from __future__ import annotations

from typing import Any

import pytest

from argus.gate.killswitch import (
    KillContext,
    conflicts_with_plan,
    evaluate_plan_rules,
    parse_rule,
    portfolio_kill,
)


def _ctx(**over: object) -> KillContext:
    base: dict[str, object] = {
        "last_closed_price": 100.0,
        "funding_8h": 0.0001,
        "real_yield_10y": 1.9,
        "event_outcomes": {},
    }
    base.update(over)
    return KillContext(**base)  # type: ignore[arg-type]


def test_price_close_rules_fire_on_the_closed_candle() -> None:
    rule = parse_rule({"kind": "price_close_above", "level": 105, "timeframe": "1d"})
    assert not evaluate_plan_rules([rule], _ctx(last_closed_price=104.9))
    fired = evaluate_plan_rules([rule], _ctx(last_closed_price=105.1))
    assert fired and "105" in fired[0].detail


def test_price_close_below_rule() -> None:
    rule = parse_rule({"kind": "price_close_below", "level": 95})
    assert evaluate_plan_rules([rule], _ctx(last_closed_price=94.0))
    assert not evaluate_plan_rules([rule], _ctx(last_closed_price=96.0))


def test_funding_flip_fires_when_the_sign_turns() -> None:
    rule = parse_rule({"kind": "funding_flip", "sign": "negative"})
    assert evaluate_plan_rules([rule], _ctx(funding_8h=-0.00002))
    assert not evaluate_plan_rules([rule], _ctx(funding_8h=0.00002))


def test_real_yield_tripwire() -> None:
    rule = parse_rule({"kind": "real_yield_above", "level": 2.5})
    assert evaluate_plan_rules([rule], _ctx(real_yield_10y=2.6))
    assert not evaluate_plan_rules([rule], _ctx(real_yield_10y=2.4))
    # Unknown macro never fires a rule: it becomes a data gap elsewhere.
    assert not evaluate_plan_rules([rule], _ctx(real_yield_10y=None))


def test_event_outcome_rule() -> None:
    rule = parse_rule({"kind": "event", "event": "fomc", "outcome": "hike"})
    assert evaluate_plan_rules([rule], _ctx(event_outcomes={"fomc": "hike"}))
    assert not evaluate_plan_rules([rule], _ctx(event_outcomes={"fomc": "hold"}))


def test_an_unparseable_rule_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValueError, match="unknown kill-switch"):
        parse_rule({"kind": "vibes_turn_bad"})
    with pytest.raises(ValueError, match="level"):
        parse_rule({"kind": "price_close_above"})


@pytest.mark.parametrize(
    "raw",
    [
        # never fire: a NaN level compares False with every price
        {"kind": "price_close_below", "level": float("nan")},
        {"kind": "price_close_above", "level": float("inf")},
        {"kind": "price_close_below", "level": -5},
        {"kind": "price_close_above", "level": True},  # a bool is not a level
        # always fire: no sign meant "fires whenever funding >= 0"
        {"kind": "funding_flip"},
        {"kind": "funding_flip", "sign": "sideways"},
        # never fire: an event rule with nothing to match
        {"kind": "event"},
        {"kind": "event", "event": "fomc"},
        # evaluated on a timeframe nothing measures
        {"kind": "price_close_below", "level": 95, "timeframe": "3m"},
    ],
)
def test_rules_that_could_never_or_always_fire_are_refused(raw: dict[str, Any]) -> None:
    """G-16: parse_rule accepted NaN/inf/negative/bool levels, a missing funding sign (which
    then fired on any non-negative funding), and event rules with no event to match."""
    with pytest.raises(ValueError):
        parse_rule(raw)


def test_a_rule_that_is_not_a_mapping_is_refused_with_value_error() -> None:
    with pytest.raises(ValueError):
        parse_rule(["price_close_below", 95])  # type: ignore[arg-type]


def test_gate_11_blocks_a_plan_that_contradicts_an_active_kill_switch() -> None:
    fired = evaluate_plan_rules(
        [parse_rule({"kind": "price_close_above", "level": 105})],
        _ctx(last_closed_price=106),
        "short",  # the open position whose ceiling broke
    )
    assert conflicts_with_plan("short", fired)  # BTC closed above the ceiling: no new shorts
    assert not conflicts_with_plan("long", fired)


def test_portfolio_drawdown_closes_everything_and_halts() -> None:
    kill = portfolio_kill(equity=850.0, peak_30d=1000.0, margin_ratio=None)
    assert kill.close_all and kill.halt
    assert "15" in kill.reasons[0]


def test_drawdown_inside_the_limit_does_nothing() -> None:
    kill = portfolio_kill(equity=900.0, peak_30d=1000.0, margin_ratio=None)
    assert not kill.close_all and not kill.halt and not kill.reduce


def test_thin_margin_asks_for_a_reduction_without_halting() -> None:
    kill = portfolio_kill(equity=1000.0, peak_30d=1000.0, margin_ratio=150.0)
    assert kill.reduce and not kill.halt
    assert "margin ratio" in kill.reasons[0]
