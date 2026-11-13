"""Protections — gate 9 (DECISION_LOGIC.md §6).

Freqtrade-style guards, with one correction from the case study (FAILURE_LOG F-029): cooldowns
and caps count **attempts**, not fills — counting successes let three failed buys through in
minutes. Every limit here is a portfolio-level brake the LLM cannot see or argue with.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits

DAILY_LOSS_LIMIT_PCT = -3.0
WEEKLY_LOSS_LIMIT_PCT = -6.0
SYMBOL_STOPOUT_WINDOW_DAYS = 5
SYMBOL_STOPOUT_LIMIT = 2
CONSECUTIVE_LOSS_LIMIT = LIVE_CONSERVATIVE.consecutive_loss_pause  # 3 (gate/limits.py)
LOSS_PAUSE_HOURS = 24
REDUCED_SIZE_TRADES = 3
REDUCED_SIZE_MULT = 0.5
MAX_ENTRIES_PER_RUN = LIVE_CONSERVATIVE.max_entries_per_run  # 2
MAX_ENTRIES_PER_DAY = LIVE_CONSERVATIVE.max_entries_per_day  # 3


@dataclass(frozen=True)
class TradingHistory:
    daily_pnl_pct: float
    weekly_pnl_pct: float
    consecutive_losses: int
    entries_this_run: int
    entries_today: int
    stop_outs_by_symbol: dict[str, list[datetime]]
    last_loss_at: datetime | None
    trades_since_pause: int
    # When the most recent losing streak reached the pause limit (G-13b). The 24 h pause and the
    # three reduced trades after it are counted from here, whatever the streak is now — a win
    # ends a streak, it does not end the reduced-size period.
    pause_started_at: datetime | None = None


@dataclass
class ProtectionState:
    allowed: bool = True
    size_mult: float = 1.0
    halt: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "size_mult": self.size_mult,
            "halt": self.halt,
            "reasons": list(self.reasons),
        }


def history_problems(history: TradingHistory) -> list[str]:
    """Impossible values in the trading history. A history that cannot be trusted cannot clear
    an entry: NaN compares False against every loss limit, a negative count never reaches one."""
    problems: list[str] = []
    for name in ("daily_pnl_pct", "weekly_pnl_pct"):
        value = getattr(history, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            problems.append(f"{name} {value!r} is not a finite number")
    for name in ("consecutive_losses", "entries_this_run", "entries_today", "trades_since_pause"):
        value = getattr(history, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"{name} {value!r} is not a non-negative count")
    return problems


def check_protections(
    symbol: str,
    history: TradingHistory,
    now: datetime,
    limits: DecisionLimits = LIVE_CONSERVATIVE,
) -> ProtectionState:
    state = ProtectionState()
    pause_at = limits.consecutive_loss_pause

    bad = history_problems(history)
    if bad:
        state.allowed = False
        state.reasons.append("trading history invalid - " + "; ".join(bad))
        return state

    if history.weekly_pnl_pct <= WEEKLY_LOSS_LIMIT_PCT:
        state.allowed = False
        state.halt = True
        state.reasons.append(
            f"weekly loss {history.weekly_pnl_pct:.1f}% <= {WEEKLY_LOSS_LIMIT_PCT:.0f}% — HALT"
        )
        return state

    if history.daily_pnl_pct <= DAILY_LOSS_LIMIT_PCT:
        state.allowed = False
        state.reasons.append(
            f"daily loss {history.daily_pnl_pct:.1f}% <= {DAILY_LOSS_LIMIT_PCT:.0f}% "
            "— no new entries today"
        )
        return state

    # Keys normalised here, not only the lookup (G-13d): a history keyed "sol" never cooled down.
    stop_outs = {key.upper(): times for key, times in history.stop_outs_by_symbol.items()}
    recent = [
        at
        for at in stop_outs.get(symbol.upper(), [])
        if now - at <= timedelta(days=SYMBOL_STOPOUT_WINDOW_DAYS)
    ]
    if len(recent) >= SYMBOL_STOPOUT_LIMIT:
        state.allowed = False
        state.reasons.append(
            f"{len(recent)} stop-outs on {symbol} in {SYMBOL_STOPOUT_WINDOW_DAYS}d — cooldown"
        )
        return state

    anchor = history.pause_started_at
    if history.consecutive_losses >= pause_at:
        if history.last_loss_at is None:
            # G-13a: an unknown time for the last loss skipped the pause. Unknown is not "long
            # ago": the pause holds until the history can say when it started.
            state.allowed = False
            state.reasons.append(
                f"{history.consecutive_losses} consecutive losses and no time for the last one "
                "— paused until the history can say when it ends"
            )
            return state
        if anchor is None or history.last_loss_at > anchor:
            anchor = history.last_loss_at
    if anchor is not None:
        since = now - anchor
        if since < timedelta(hours=LOSS_PAUSE_HOURS):
            state.allowed = False
            hours_left = LOSS_PAUSE_HOURS - since.total_seconds() / 3600
            state.reasons.append(
                f"{pause_at}+ consecutive losses — 24h pause ({hours_left:.0f}h left)"
            )
            return state
        if history.trades_since_pause < REDUCED_SIZE_TRADES:
            state.size_mult *= REDUCED_SIZE_MULT
            state.reasons.append(
                f"after a {pause_at}-loss pause — size ×{REDUCED_SIZE_MULT} "
                f"for {REDUCED_SIZE_TRADES - history.trades_since_pause} more trade(s)"
            )

    if history.entries_this_run >= limits.max_entries_per_run:
        state.allowed = False
        state.reasons.insert(
            0, f"{history.entries_this_run} entries this run (max {limits.max_entries_per_run})"
        )
        return state
    if history.entries_today >= limits.max_entries_per_day:
        state.allowed = False
        state.reasons.insert(
            0, f"{history.entries_today} entries today (max {limits.max_entries_per_day})"
        )
        return state
    return state
