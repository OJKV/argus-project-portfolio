"""Deterministic facts about a proposed trade against the current book.

The F-102 lesson, applied to the book instead of the position: Gemini got heat, events and a
recent stop-out right and missed correlation — the one that needs arithmetic across several open
positions. Counting same-direction high-beta exposure is not judgement, so it is computed here
and handed to Risk Debate as a fact.
"""

from __future__ import annotations

from typing import Any

# The gate's own numbers (review D-N2): this module had HIGH_BETA = 1.0 against the gate's 1.3,
# so it called positions correlated that gate 6 would not, and read its heat budget from a key
# the live book never carried. One book, one set of limits.
from argus.gate.rules import HIGH_BETA, MAX_OPEN_RISK_PCT

HEAT_HEADROOM_WARN = 0.25  # under a quarter of the budget left
RECENT_STOP_HOURS = 96


def compute_risk_flags(
    plan: dict[str, Any],
    portfolio: dict[str, Any],
    calendar: list[dict[str, Any]] | None = None,
    budget_pct: float = MAX_OPEN_RISK_PCT,
) -> dict[str, Any]:
    """`portfolio` is the dict `argus.runs.manager.risk_book` builds from the gate's own book.

    `budget_pct` is the open-risk ceiling gate 6 enforces for this account's profile."""
    side = str(plan.get("side", "long"))
    symbol = str(plan.get("symbol", "")).upper()
    horizon = float(plan.get("time_stop_hours") or 0)

    open_positions = portfolio.get("open_positions") or []
    same_way = [
        p
        for p in open_positions
        if str(p.get("side")) == side and str(p.get("symbol", "")).upper() != symbol
    ]
    # Unknown beta counts as high, as it does in gate 6: correlation nobody measured is still
    # correlation.
    correlated = [
        p for p in same_way if p.get("beta_btc") is None or float(p["beta_btc"]) > HIGH_BETA
    ]

    heat = float(portfolio.get("open_risk_pct") or 0.0)
    equity = float(portfolio.get("equity_usd") or 0.0)
    trade_risk = float((plan.get("sized") or {}).get("risk_usd") or 0.0)
    trade_risk_pct = trade_risk / equity * 100 if equity > 0 else 0.0
    budget = budget_pct
    headroom = (budget - heat) / budget if budget > 0 else 1.0

    events = [
        e for e in (calendar or []) if horizon and 0 <= float(e.get("when_hours", 1e9)) <= horizon
    ]
    stops = [
        s
        for s in (portfolio.get("recent_stop_outs") or [])
        if str(s.get("symbol", "")).upper() == symbol
        and float(s.get("hours_ago", 1e9)) <= RECENT_STOP_HOURS
    ]

    return {
        "correlated_open_positions": [p.get("symbol") for p in correlated],
        "correlated_exposure": len(correlated) >= 1,
        "heat_headroom_fraction": round(headroom, 3),
        "book_nearly_full": headroom <= HEAT_HEADROOM_WARN,
        # The decisive fact for DECLINE rather than REDUCE: this trade does not fit in what
        # is left of the budget even at the size the gate chose.
        "would_exceed_heat_budget": budget > 0 and heat + trade_risk_pct > budget,
        "trade_risk_pct": round(trade_risk_pct, 3),
        "events_inside_time_stop": [e.get("what") for e in events],
        "recent_stop_outs_on_symbol": len(stops),
    }
