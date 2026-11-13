"""`doctor --auth` must fail an account that cannot trade perpetuals (live finding 2026-09-17)."""

from __future__ import annotations

from argus.doctor import account_mode_check


def test_simple_mode_cannot_trade_swaps() -> None:
    # The new demo sub-account came back acctLv=1; every order failed with sCode 51010
    # "You can't complete this request under your current account mode".
    check = account_mode_check("demo", "1")
    assert not check.ok
    assert "Single-currency margin" in check.detail


def test_margin_modes_that_trade_swaps_pass() -> None:
    for level in ("2", "3", "4"):
        assert account_mode_check("demo", level).ok


def test_an_unknown_level_fails_closed() -> None:
    assert not account_mode_check("live", None).ok
