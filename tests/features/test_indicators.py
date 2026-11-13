"""Indicators on synthetic OHLCV where the right answer is known in advance."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from argus.features import indicators as ind


def _frame(
    close: list[float] | np.ndarray[Any, Any], spread: float = 1.0, volume: float = 100.0
) -> pd.DataFrame:
    c = pd.Series(np.asarray(close, dtype=float))
    return pd.DataFrame(
        {"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": volume}
    )


def _trend(n: int = 300, step: float = 1.0, start: float = 100.0) -> np.ndarray[Any, Any]:
    return start + step * np.arange(n)


def test_okx_candles_become_an_ascending_frame_without_the_forming_bar() -> None:
    rows = [  # OKX: newest first; confirm "0" = still forming
        ["1789567200000", "3", "4", "2", "3.5", "10", "35", "350", "0"],
        ["1789563600000", "2", "3", "1", "3", "10", "30", "300", "1"],
        ["1789560000000", "1", "2", "0.5", "2", "10", "20", "200", "1"],
    ]
    frame = ind.candles_to_frame(rows)
    assert list(frame["close"]) == [2.0, 3.0]
    assert frame.index.is_monotonic_increasing
    assert list(frame["turnover"]) == [200.0, 300.0]


def test_ema_stack_orders_correctly_in_an_uptrend() -> None:
    stack = ind.ema_stack(_frame(_trend()))
    assert stack is not None
    assert stack["close"] > stack["ema20"] > stack["ema50"] > stack["ema200"]
    assert stack["slope20"] > 0
    assert stack["order"] == "bull"


def test_ema_stack_needs_enough_history() -> None:
    assert ind.ema_stack(_frame(_trend(30))) is None


def test_rsi_extremes() -> None:
    up, down = ind.rsi(_frame(_trend(100))), ind.rsi(_frame(_trend(100, step=-1, start=500)))
    assert up is not None and up > 95
    assert down is not None and down < 5


def test_atr_equals_the_constant_bar_range() -> None:
    flat = _frame([100.0] * 60, spread=2.0)  # every bar spans 98–102
    atr = ind.atr(flat)
    assert atr is not None
    assert atr["atr"] == pytest.approx(4.0, rel=1e-6)
    assert atr["atr_pct"] == pytest.approx(4.0, rel=1e-6)


def test_adx_separates_trend_from_noise() -> None:
    rng = np.random.default_rng(7)
    trending = ind.adx(_frame(_trend(200)))
    noisy = ind.adx(_frame(100 + rng.standard_normal(200).cumsum() * 0.2))
    assert trending is not None and noisy is not None
    assert trending > 40 > noisy


def test_bollinger_position_is_high_at_the_top_of_a_range() -> None:
    base = list(100 + np.sin(np.arange(60) / 3))
    pos = ind.bollinger_position(_frame([*base, 104.0]))
    assert pos is not None and pos > 0.9


def test_swing_structure_finds_the_window_extremes() -> None:
    close = [100.0] * 60
    close[45] = 120.0
    close[50] = 80.0
    structure = ind.swing_structure(_frame(close, spread=0.0))
    assert structure["high_20"] == 120.0
    assert structure["low_20"] == 80.0
    assert structure["high_50"] == 120.0


def test_vwap_weights_by_volume() -> None:
    frame = _frame([10.0, 20.0], spread=0.0)
    frame["volume"] = [3.0, 1.0]
    assert ind.vwap(frame, bars=2) == pytest.approx((10 * 3 + 20 * 1) / 4)


def test_relative_volume_compares_the_last_bar_to_its_average() -> None:
    frame = _frame([100.0] * 25)
    frame.loc[24, "volume"] = 200.0
    assert ind.relative_volume(frame, lookback=20) == pytest.approx(2.0)


def test_period_returns() -> None:
    frame = _frame(_trend(40, step=1.0, start=100.0))  # daily bars
    rets = ind.period_returns(frame, (7, 30))
    assert rets["ret_7"] == pytest.approx((139 / 132 - 1) * 100)
    assert rets["ret_30"] == pytest.approx((139 / 109 - 1) * 100)


def test_beta_to_btc_recovers_a_known_multiplier() -> None:
    rng = np.random.default_rng(1)
    btc_ret = rng.normal(0, 0.02, 60)
    btc = 100 * np.cumprod(1 + btc_ret)
    alt = 50 * np.cumprod(1 + 2 * btc_ret)
    beta = ind.beta(_frame(alt), _frame(btc), window=30)
    assert beta == pytest.approx(2.0, abs=0.05)


def test_beta_pairs_returns_by_timestamp_not_by_position() -> None:
    """D-13: a run spanning the daily close can see one more closed bar for the asset than for
    BTC. Pairing by position then regressed each asset day on the *previous* BTC day, beta
    came out near 0, and the same-side high-beta cap never counted the asset."""
    rng = np.random.default_rng(1)
    btc_ret = rng.normal(0, 0.02, 61)
    days = pd.date_range("2026-08-01", periods=61, freq="D", tz="UTC")
    btc = _frame(100 * np.cumprod(1 + btc_ret)).set_index(days)
    alt = _frame(50 * np.cumprod(1 + 2 * btc_ret)).set_index(days)
    beta = ind.beta(alt, btc.iloc[:-1], window=30)  # BTC fetched before the newest close
    assert beta == pytest.approx(2.0, abs=0.05)


def test_beta_needs_a_full_window_of_shared_bars() -> None:
    days = pd.date_range("2026-08-01", periods=40, freq="D", tz="UTC")
    btc = _frame(_trend(40)).set_index(days)
    alt = _frame(_trend(40)).set_index(days + pd.Timedelta(days=25))  # 15 days overlap
    assert ind.beta(alt, btc, window=30) is None


def test_realized_vol_percentile_is_high_after_a_volatility_spike() -> None:
    rng = np.random.default_rng(3)
    calm = rng.normal(0, 0.005, 340)
    wild = rng.normal(0, 0.05, 25)
    close = 100 * np.cumprod(1 + np.concatenate([calm, wild]))
    pct = ind.realized_vol_percentile(_frame(close), window=20, lookback=365)
    assert pct is not None and pct > 90


def test_every_indicator_returns_none_on_empty_input_instead_of_raising() -> None:
    empty = _frame([])
    assert ind.ema_stack(empty) is None
    assert ind.rsi(empty) is None
    assert ind.atr(empty) is None
    assert ind.adx(empty) is None
    assert ind.bollinger_position(empty) is None
    assert ind.vwap(empty) is None
    assert ind.relative_volume(empty) is None
    assert ind.realized_vol_percentile(empty) is None
    assert ind.beta(empty, empty) is None


def test_band_width_argument_is_actually_applied() -> None:
    # pandas-ta 0.4 renamed `std` → `lower_std`/`upper_std` and swallows the old name via
    # **kwargs, silently using 2.0. Narrower bands must move the same close further out.
    base = list(100 + np.sin(np.arange(60) / 3))
    frame = _frame([*base, 101.5])
    wide = ind.bollinger_position(frame, std=3.0)
    narrow = ind.bollinger_position(frame, std=1.0)
    assert wide is not None and narrow is not None
    assert narrow > wide
