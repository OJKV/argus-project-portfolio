"""Rolling per-asset memory card (ARCHITECTURE.md §11; UNIVERSE_OPTIONS.md).

Deterministic and rebuilt every Manager run: typical volatility, funding regime, unlock cadence,
revenue-to-token mechanics, and the last few theses for the asset from the plan ledger. This is
what makes researching a fixed core set cheaper each run. Thin history leaves a field out rather
than inventing it.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import pandas_ta as ta
from sqlalchemy import Engine, text

MIN_DAILY_BARS = 30
MIN_FUNDING_POINTS = 10
THESES_KEPT = 5


def build_card(
    symbol: str,
    daily: pd.DataFrame,
    funding_rates: list[float],
    catalysts: list[dict[str, Any]],
    tokenomics: dict[str, Any],
    theses: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    card: dict[str, Any] = {}

    if len(daily) >= MIN_DAILY_BARS:
        atr = ta.atr(daily["high"], daily["low"], daily["close"], length=14)
        pct = (atr / daily["close"] * 100).dropna().iloc[-90:]
        if not pct.empty:
            card["atr_pct_med_90d"] = round(float(pct.median()), 2)

    if len(funding_rates) >= MIN_FUNDING_POINTS:
        card["funding_med_30d%"] = round(statistics.median(funding_rates) * 100, 4)
        latest = funding_rates[-1]
        card["funding_now_pctile"] = round(
            sum(r <= latest for r in funding_rates) / len(funding_rates) * 100, 1
        )

    unlock_days = sorted(
        c["date"]
        for c in catalysts
        if str(c.get("symbol", "")).upper() == symbol.upper()
        and c.get("kind") == "unlock"
        and isinstance(c.get("date"), date)
    )
    upcoming = [d for d in unlock_days if d >= today]
    recent = [d for d in unlock_days if today - timedelta(days=90) <= d < today]
    if upcoming:
        card["next_unlock"] = upcoming[0].isoformat()
    if recent:
        card["unlocks_last_90d"] = len(recent)  # cadence: how often it actually happens

    if "token_revenue_share" in tokenomics:
        card["rev_to_token"] = tokenomics["token_revenue_share"]
    if theses:
        card["theses"] = theses[:THESES_KEPT]
    return card


def recent_theses(engine: Engine, symbol: str, limit: int = THESES_KEPT) -> list[dict[str, Any]]:
    """Latest plans for the asset, newest first (outcomes join in once trades close)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT side, state, created_at FROM plans WHERE symbol = :s "
                "ORDER BY created_at DESC, rowid DESC LIMIT :n"
            ),
            {"s": symbol.upper(), "n": limit},
        ).mappings()
        return [dict(r) for r in rows]


def save_card(engine: Engine, symbol: str, card: dict[str, Any], now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO asset_memory (symbol, updated_at, card) VALUES (:s, :t, :c) "
                "ON CONFLICT(symbol) DO UPDATE SET updated_at = excluded.updated_at, "
                "card = excluded.card"
            ),
            {"s": symbol.upper(), "t": now.isoformat(), "c": json.dumps(card, default=str)},
        )


def load_card(engine: Engine, symbol: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT card FROM asset_memory WHERE symbol = :s"), {"s": symbol.upper()}
        ).scalar()
    return json.loads(row) if row else None
