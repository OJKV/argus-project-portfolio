"""Facts about a trade against the book, computed rather than inferred."""

from __future__ import annotations

from argus.features.risk_flags import compute_risk_flags

PLAN = {"symbol": "SOL", "side": "long", "time_stop_hours": 120}


def test_same_direction_high_beta_positions_are_correlated() -> None:
    flags = compute_risk_flags(
        PLAN,
        {
            "open_positions": [
                {"symbol": "ETH", "side": "long", "beta_btc": 1.5},
                {"symbol": "BTC", "side": "short", "beta_btc": 1.0},
            ]
        },
    )
    assert flags["correlated_open_positions"] == ["ETH"]
    assert flags["correlated_exposure"] is True


def test_an_opposite_position_is_a_hedge_not_a_correlation() -> None:
    flags = compute_risk_flags(PLAN, {"open_positions": [{"symbol": "ETH", "side": "short"}]})
    assert flags["correlated_exposure"] is False


def test_the_same_symbol_is_not_counted_as_correlated_with_itself() -> None:
    flags = compute_risk_flags(PLAN, {"open_positions": [{"symbol": "SOL", "side": "long"}]})
    assert flags["correlated_exposure"] is False


def test_a_book_with_little_heat_left_is_flagged() -> None:
    # The live book's keys (review D-N2); the budget is the gate's 5 %, never sent to the agent.
    assert compute_risk_flags(PLAN, {"open_risk_pct": 4.5})["book_nearly_full"]
    assert not compute_risk_flags(PLAN, {"open_risk_pct": 1.0})["book_nearly_full"]


def test_an_unmeasured_beta_counts_as_correlated_like_gate_6() -> None:
    flags = compute_risk_flags(PLAN, {"open_positions": [{"symbol": "ETH", "side": "long"}]})
    assert flags["correlated_open_positions"] == ["ETH"]


def test_only_events_inside_the_time_stop_count() -> None:
    cal = [{"when_hours": 30, "what": "CPI"}, {"when_hours": 300, "what": "FOMC"}]
    assert compute_risk_flags(PLAN, {}, cal)["events_inside_time_stop"] == ["CPI"]


def test_recent_stop_outs_are_counted_per_symbol() -> None:
    pf = {
        "recent_stop_outs": [
            {"symbol": "SOL", "hours_ago": 30},
            {"symbol": "SOL", "hours_ago": 200},
            {"symbol": "ETH", "hours_ago": 5},
        ]
    }
    assert compute_risk_flags(PLAN, pf)["recent_stop_outs_on_symbol"] == 1


def test_an_empty_book_raises_nothing() -> None:
    flags = compute_risk_flags(PLAN, {})
    assert flags["correlated_exposure"] is False
    assert flags["book_nearly_full"] is False
    assert flags["recent_stop_outs_on_symbol"] == 0


def test_a_trade_that_does_not_fit_the_remaining_budget_is_flagged() -> None:
    """The fact that separates DECLINE from REDUCE: 1% of new risk on top of 4.5% heat breaks
    the gate's 5% budget however the trade is sized."""
    plan = {**PLAN, "sized": {"risk_usd": 50.0}}
    full = compute_risk_flags(plan, {"equity_usd": 5000, "open_risk_pct": 4.5})
    assert full["would_exceed_heat_budget"] is True
    roomy = compute_risk_flags(plan, {"equity_usd": 5000, "open_risk_pct": 2.0})
    assert roomy["would_exceed_heat_budget"] is False
