"""Technical indicators for the dossier and the regime (ARCHITECTURE.md §5).

Deterministic, no LLM. Every function takes an ascending OHLCV frame and returns a plain number
(or a small dict) — or `None` when the history is too short. Nothing here raises on thin data:
a missing indicator becomes a dossier gap, not a crashed run.

pandas-ta computes the Wilder-smoothed series (RSI, ADX, ATR) and Bollinger bands; its output
column names change between versions, so columns are selected by prefix, never by full name.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta

OHLCV = ["open", "high", "low", "close", "volume"]


def candles_to_frame(rows: list[list[Any]]) -> pd.DataFrame:
    """OKX candle rows (newest first) → ascending frame of *closed* bars.

    Row layout: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]; confirm "0" marks the bar
    that is still forming, which would make every close-based indicator lie.
    """
    closed = [r for r in rows if len(r) < 9 or str(r[8]) == "1"]
    if not closed:
        return pd.DataFrame(columns=[*OHLCV, "turnover"])
    frame = pd.DataFrame(
        {
            "ts": pd.to_datetime([int(r[0]) for r in closed], unit="ms", utc=True),
            "open": [float(r[1]) for r in closed],
            "high": [float(r[2]) for r in closed],
            "low": [float(r[3]) for r in closed],
            "close": [float(r[4]) for r in closed],
            "volume": [float(r[5]) for r in closed],
            "turnover": [float(r[7]) if len(r) > 7 and r[7] != "" else np.nan for r in closed],
        }
    )
    return frame.sort_values("ts").set_index("ts")


def _last(series: pd.Series | None) -> float | None:
    if series is None or series.empty:
        return None
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def _column(frame: pd.DataFrame | None, prefix: str) -> pd.Series | None:
    if frame is None:
        return None
    for name in frame.columns:
        if str(name).startswith(prefix):
            return frame[name]
    return None


def ema_stack(frame: pd.DataFrame, slope_bars: int = 5) -> dict[str, Any] | None:
    """EMA 20/50/200, the EMA20 slope (% over `slope_bars`), and the stack order."""
    if len(frame) < 200:
        return None
    close = frame["close"]
    ema20, ema50, ema200 = (ta.ema(close, length=n) for n in (20, 50, 200))
    e20, e50, e200 = _last(ema20), _last(ema50), _last(ema200)
    if None in (e20, e50, e200) or ema20 is None or len(ema20.dropna()) <= slope_bars:
        return None
    prior = float(ema20.iloc[-1 - slope_bars])
    slope = (float(ema20.iloc[-1]) - prior) / prior * 100 if prior else 0.0
    last = float(close.iloc[-1])
    assert e20 is not None and e50 is not None and e200 is not None
    if last > e20 > e50 > e200:
        order = "bull"
    elif last < e20 < e50 < e200:
        order = "bear"
    else:
        order = "mixed"
    return {
        "close": last,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "slope20": slope,
        "order": order,
    }


def rsi(frame: pd.DataFrame, length: int = 14) -> float | None:
    if len(frame) <= length:
        return None
    return _last(ta.rsi(frame["close"], length=length))


def atr(frame: pd.DataFrame, length: int = 14) -> dict[str, float] | None:
    """ATR in price units and as a % of the last close."""
    if len(frame) <= length:
        return None
    value = _last(ta.atr(frame["high"], frame["low"], frame["close"], length=length))
    close = float(frame["close"].iloc[-1])
    if value is None or close <= 0:
        return None
    return {"atr": value, "atr_pct": value / close * 100}


def adx(frame: pd.DataFrame, length: int = 14) -> float | None:
    if len(frame) <= length * 2:
        return None
    return _last(
        _column(ta.adx(frame["high"], frame["low"], frame["close"], length=length), "ADX_")
    )


def bollinger_position(frame: pd.DataFrame, length: int = 20, std: float = 2.0) -> float | None:
    """Where the close sits in the band: 0 = lower band, 1 = upper band (can exceed both)."""
    if len(frame) < length:
        return None
    bands = ta.bbands(frame["close"], length=length, lower_std=std, upper_std=std)
    lower, upper = _last(_column(bands, "BBL_")), _last(_column(bands, "BBU_"))
    if lower is None or upper is None or upper == lower:
        return None
    return (float(frame["close"].iloc[-1]) - lower) / (upper - lower)


def swing_structure(frame: pd.DataFrame, windows: tuple[int, ...] = (20, 50)) -> dict[str, float]:
    """Highest high / lowest low over each window — the structure levels stops sit beyond."""
    out: dict[str, float] = {}
    for n in windows:
        if len(frame) >= n:
            tail = frame.iloc[-n:]
            out[f"high_{n}"] = float(tail["high"].max())
            out[f"low_{n}"] = float(tail["low"].min())
    return out


def vwap(frame: pd.DataFrame, bars: int = 24) -> float | None:
    """Volume-weighted typical price over the last `bars` (24 × 1h = the daily VWAP)."""
    if frame.empty:
        return None
    tail = frame.iloc[-bars:]
    volume = tail["volume"].sum()
    if volume <= 0:
        return None
    typical = (tail["high"] + tail["low"] + tail["close"]) / 3
    return float((typical * tail["volume"]).sum() / volume)


def relative_volume(frame: pd.DataFrame, lookback: int = 20) -> float | None:
    """Last closed bar's volume divided by the mean of the `lookback` bars before it."""
    if len(frame) <= lookback:
        return None
    base = frame["volume"].iloc[-1 - lookback : -1].mean()
    if not base or pd.isna(base):
        return None
    return float(frame["volume"].iloc[-1] / base)


def period_returns(frame: pd.DataFrame, periods: tuple[int, ...] = (7, 30)) -> dict[str, float]:
    """Percent return over each period, in bars (daily frame → days)."""
    out: dict[str, float] = {}
    close = frame["close"]
    for n in periods:
        if len(close) > n and close.iloc[-1 - n]:
            out[f"ret_{n}"] = float((close.iloc[-1] / close.iloc[-1 - n] - 1) * 100)
    return out


def _returns(frame: pd.DataFrame) -> pd.Series:
    return frame["close"].pct_change().dropna()


def beta(asset: pd.DataFrame, btc: pd.DataFrame, window: int = 30) -> float | None:
    """Sensitivity of the asset's returns to BTC's over the last `window` shared bars.

    Closes are inner-joined on the bar timestamp before returns are taken (D-13): pairing by
    position regressed each asset bar on the *previous* BTC bar whenever one frame had a bar
    the other lacked (a run spanning the daily close), which reads as beta ≈ 0.
    """
    if asset.empty or btc.empty:
        return None
    closes = pd.concat([asset["close"], btc["close"]], axis=1, join="inner").dropna()
    rets = closes.pct_change().dropna().iloc[-window:]
    if len(rets) < max(window, 2):
        return None
    a, b = rets.iloc[:, 0].to_numpy(), rets.iloc[:, 1].to_numpy()
    variance = float(np.var(b, ddof=1))
    if variance == 0:
        return None
    return float(np.cov(a, b, ddof=1)[0, 1] / variance)


def realized_vol_percentile(
    frame: pd.DataFrame, window: int = 20, lookback: int = 365
) -> float | None:
    """Percentile of today's `window`-bar realized vol within the last `lookback` bars (0–100)."""
    if len(frame) < window * 3:
        return None
    rv = _returns(frame).rolling(window).std().dropna().iloc[-lookback:]
    if rv.empty:
        return None
    return float((rv <= rv.iloc[-1]).mean() * 100)


def distance_pct(price: float, level: float | None) -> float | None:
    if level is None or level == 0:
        return None
    return (price / level - 1) * 100
