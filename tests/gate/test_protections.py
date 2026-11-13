"""Protections — gate 9 (DECISION_LOGIC.md §6; FAILURE_LOG F-029, freqtrade-style guards)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from argus.gate.protections import ProtectionState, TradingHistory, check_protections

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def _history(**over: object) -> TradingHistory:
    base: dict[str, object] = {
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
    return TradingHistory(**base)  # type: ignore[arg-type]


def test_a_clean_book_allows_the_entry_at_full_size() -> None:
    state = check_protections("SOL", _history(), NOW)
    assert state.allowed and state.size_mult == 1.0 and state.reasons == []


def test_daily_loss_limit_stops_new_entries_for_the_day() -> None:
    state = check_protections("SOL", _history(daily_pnl_pct=-3.2), NOW)
    assert not state.allowed and "daily loss" in state.reasons[0]


def test_weekly_loss_limit_demands_a_halt() -> None:
    state = check_protections("SOL", _history(weekly_pnl_pct=-6.5), NOW)
    assert not state.allowed and state.halt
    assert "weekly loss" in state.reasons[0]


def test_two_stop_outs_in_the_same_symbol_trigger_a_five_day_cooldown() -> None:
    history = _history(
        stop_outs_by_symbol={"SOL": [NOW - timedelta(days=1), NOW - timedelta(days=3)]}
    )
    assert not check_protections("SOL", history, NOW).allowed
    assert check_protections("ETH", history, NOW).allowed  # the cooldown is per symbol


def test_old_stop_outs_fall_out_of_the_window() -> None:
    history = _history(
        stop_outs_by_symbol={"SOL": [NOW - timedelta(days=6), NOW - timedelta(days=9)]}
    )
    assert check_protections("SOL", history, NOW).allowed


def test_three_consecutive_losses_pause_for_a_day_then_halve_size() -> None:
    paused = check_protections(
        "SOL", _history(consecutive_losses=3, last_loss_at=NOW - timedelta(hours=6)), NOW
    )
    assert not paused.allowed and "24h pause" in paused.reasons[0]

    after = check_protections(
        "SOL",
        _history(
            consecutive_losses=3, last_loss_at=NOW - timedelta(hours=30), trades_since_pause=1
        ),
        NOW,
    )
    assert after.allowed and after.size_mult == 0.5


def test_size_returns_to_full_after_three_trades_post_pause() -> None:
    state = check_protections(
        "SOL",
        _history(
            consecutive_losses=3, last_loss_at=NOW - timedelta(hours=30), trades_since_pause=3
        ),
        NOW,
    )
    assert state.allowed and state.size_mult == 1.0


@pytest.mark.parametrize(("per_run", "per_day"), [(2, 0), (0, 3)])
def test_entry_caps_per_run_and_per_day(per_run: int, per_day: int) -> None:
    state = check_protections("SOL", _history(entries_this_run=per_run, entries_today=per_day), NOW)
    assert not state.allowed
    assert "entries" in state.reasons[0]


def test_cooldowns_count_attempts_not_fills() -> None:
    # F-029: counting successes let three failed buys through in minutes.
    # G-18: this used to pass on `entries_today=3` alone, whatever the stop-out did. The counter
    # is what decides — two attempts pass, the third is refused — and the journal side (orders
    # the venue rejected still count) is pinned in tests/runs/test_manager_book.py.
    assert check_protections("SOL", _history(entries_today=2), NOW).allowed
    assert not check_protections("SOL", _history(entries_today=3), NOW).allowed
    one_stop_out = _history(stop_outs_by_symbol={"SOL": [NOW - timedelta(hours=2)]})
    assert check_protections("SOL", one_stop_out, NOW).allowed  # a single stop-out is no cooldown


def test_a_losing_streak_with_no_time_for_the_last_loss_pauses() -> None:
    """G-13a: `consecutive_losses >= 3` with `last_loss_at=None` skipped the pause — fail open."""
    state = check_protections("SOL", _history(consecutive_losses=3, last_loss_at=None), NOW)
    assert not state.allowed


def test_the_reduced_size_lasts_three_trades_even_after_a_win() -> None:
    """G-13b: x0.5 applied only while the streak stood at 3+, so the first winning reduced trade
    restored full size. The spec says three reduced trades after the pause."""
    after_a_win = _history(
        consecutive_losses=0,
        last_loss_at=NOW - timedelta(hours=40),
        pause_started_at=NOW - timedelta(hours=40),
        trades_since_pause=1,
    )
    state = check_protections("SOL", after_a_win, NOW)
    assert state.allowed and state.size_mult == pytest.approx(0.5)
    done = check_protections("SOL", _history(pause_started_at=NOW - timedelta(days=3)), NOW)
    assert done.size_mult == 1.0  # trades_since_pause 99: the three are long spent


def test_stop_out_keys_are_matched_whatever_their_case() -> None:
    """G-13d: keys were normalised on lookup only, so a history keyed "sol" never cooled down."""
    recent = [NOW - timedelta(days=1), NOW - timedelta(days=2)]
    state = check_protections("SOL", _history(stop_outs_by_symbol={"sol": recent}), NOW)
    assert not state.allowed


def test_protection_state_is_serialisable_for_the_journal() -> None:
    state: ProtectionState = check_protections("SOL", _history(daily_pnl_pct=-3.2), NOW)
    assert state.as_dict()["allowed"] is False
    assert isinstance(state.as_dict()["reasons"], list)
