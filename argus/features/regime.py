"""Market regime: rule table + modifiers (DECISION_LOGIC.md §2), and the snapshot that feeds it.

Code decides the envelope; the LLM decides inside it. The Regime Analyst's posture can only
*reduce* the multiplier (`apply_posture`).

Missing data never raises the regime. Without the event calendar, realized vol, breadth or the
BTC trend, a trend cannot be confirmed, so the label is capped at CHOP (0.5×, limit-only) and the
gap is reported — a calendar outage must not let a full-size trade through before FOMC.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from argus.data.envelope import finite_number
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits

Label = Literal["TREND_UP", "TREND_DOWN", "CHOP", "EVENT_RISK"]

BASE: dict[Label, tuple[float, bool]] = {
    "EVENT_RISK": (0.4, True),
    "TREND_UP": (1.0, False),
    "TREND_DOWN": (1.0, False),
    "CHOP": (0.5, True),
}
EVENT_WINDOW_HOURS = 24.0
VOL_SPIKE_PCTILE = 90.0
BREADTH_UP, BREADTH_DOWN = 55.0, 45.0
REAL_YIELD_TRIPWIRE = 2.50
STABLE_DRAIN_PCT = -1.0
ETF_OUTFLOW_USD = -500e6
ETF_ASSETS = frozenset({"BTC", "ETH"})

# Inputs whose absence makes a trend unconfirmable.
TREND_CRITICAL = ("btc_close", "btc_ema20", "btc_ema50", "btc_ema20_slope", "breadth_20")
EVENT_CRITICAL = ("next_event_hours", "btc_rv20_pct")
MODIFIER_INPUTS = ("real_yield_10y", "stable_supply_7d_pct", "etf_5d_usd")


NUMERIC_INPUTS = (
    "btc_close",
    "btc_ema20",
    "btc_ema50",
    "btc_ema20_slope",
    "btc_rv20_pct",
    "breadth_20",
    "funding_median",
    "oi_7d_pct",
    "stable_supply_7d_pct",
    "etf_5d_usd",
    "real_yield_10y",
    "fear_greed",
    "next_event_hours",
)


class RegimeInputs(BaseModel):
    """Regime inputs. A bool is refused; NaN/inf become None (missing), which `classify` already
    treats as unconfirmable, so a bad number can never read as data (Muse P1-3)."""

    model_config = ConfigDict(validate_assignment=True)

    btc_close: float | None = None
    btc_ema20: float | None = None
    btc_ema50: float | None = None
    btc_ema20_slope: float | None = None
    btc_rv20_pct: float | None = None
    breadth_20: float | None = None
    funding_median: float | None = None
    oi_7d_pct: float | None = None
    stable_supply_7d_pct: float | None = None
    etf_5d_usd: float | None = None
    real_yield_10y: float | None = None
    fear_greed: float | None = None
    next_event_hours: float | None = None
    next_event: str | None = None

    @field_validator(*NUMERIC_INPUTS, mode="before")
    @classmethod
    def _no_bool_no_nonfinite(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("a bool is not a number")
        if value is None:
            return None
        return finite_number(value)


@dataclass
class RegimeView:
    label: Label
    risk_mult: float
    limit_only: bool
    long_factor: float = 1.0
    etf_factor: float = 1.0
    reasons: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    inputs: RegimeInputs | None = None

    @property
    def risk_mult_long(self) -> float:
        return self.risk_mult * self.long_factor

    @property
    def risk_mult_short(self) -> float:
        return self.risk_mult

    def risk_mult_for(self, symbol: str, side: Literal["long", "short"]) -> float:
        if side == "short":
            return self.risk_mult_short
        extra = self.etf_factor if symbol.upper() in ETF_ASSETS else 1.0
        return self.risk_mult_long * extra


def _trend(i: RegimeInputs) -> Label | None:
    if None in (i.btc_close, i.btc_ema20, i.btc_ema50, i.btc_ema20_slope, i.breadth_20):
        return None
    assert i.btc_close is not None and i.btc_ema20 is not None and i.btc_ema50 is not None
    assert i.btc_ema20_slope is not None and i.breadth_20 is not None
    if i.btc_close > i.btc_ema20 > i.btc_ema50 and i.btc_ema20_slope > 0:
        if i.breadth_20 >= BREADTH_UP:
            return "TREND_UP"
    if i.btc_close < i.btc_ema20 < i.btc_ema50 and i.btc_ema20_slope < 0:
        if i.breadth_20 <= BREADTH_DOWN:
            return "TREND_DOWN"
    return None


def classify(inputs: RegimeInputs, limits: DecisionLimits = LIVE_CONSERVATIVE) -> RegimeView:
    gaps = [
        f"{name} missing"
        for name in (*EVENT_CRITICAL, *TREND_CRITICAL, *MODIFIER_INPUTS)
        if getattr(inputs, name) is None
    ]
    reasons: list[str] = []

    hours, rv = inputs.next_event_hours, inputs.btc_rv20_pct
    if hours is not None and 0 <= hours <= EVENT_WINDOW_HOURS:
        label: Label = "EVENT_RISK"
        reasons.append(f"{inputs.next_event or 'high-impact event'} in {hours:.0f}h")
    elif rv is not None and rv >= VOL_SPIKE_PCTILE:
        label = "EVENT_RISK"
        reasons.append(f"BTC 20d realized vol at the {rv:.0f}th percentile")
    elif hours is None or rv is None:
        label = "CHOP"
        reasons.append("event calendar or realized vol unavailable — trend not confirmable")
    else:
        trend = _trend(inputs)
        if trend is None:
            label = "CHOP"
            reasons.append(
                "BTC trend or breadth unavailable"
                if any(getattr(inputs, n) is None for n in TREND_CRITICAL)
                else "no clean trend (stack, slope or breadth disagree)"
            )
        else:
            label = trend
            reasons.append(f"BTC stack aligned, breadth {inputs.breadth_20:.0f}%")

    risk_mult, limit_only = BASE[label]
    if label == "CHOP" and not limits.chop_limit_only:
        # Demo exploration (Q4): market entries allowed in CHOP. The ×0.5 size stays.
        limit_only = False
    view = RegimeView(label, risk_mult, limit_only, reasons=reasons, data_gaps=gaps, inputs=inputs)

    if inputs.real_yield_10y is not None and inputs.real_yield_10y > REAL_YIELD_TRIPWIRE:
        view.long_factor *= 0.5
        view.modifiers.append(f"real yield {inputs.real_yield_10y:.2f}% > 2.50% → longs ×0.5")
    if inputs.stable_supply_7d_pct is not None and inputs.stable_supply_7d_pct < STABLE_DRAIN_PCT:
        view.long_factor *= 0.7
        view.modifiers.append(
            f"stablecoin supply {inputs.stable_supply_7d_pct:+.2f}% 7d → longs ×0.7"
        )
    if inputs.etf_5d_usd is not None and inputs.etf_5d_usd < ETF_OUTFLOW_USD:
        view.etf_factor *= 0.7
        view.modifiers.append(f"ETF flows ${inputs.etf_5d_usd / 1e6:,.0f}M 5d → BTC/ETH longs ×0.7")
    return view


def apply_posture(view: RegimeView, posture_mult: float) -> RegimeView:
    """The Regime Analyst may shrink the envelope, never widen or invert it.

    Returns a new view: applying the same posture twice (e.g. a retried LLM call) must not
    compound the reduction.
    """
    # min/max pass NaN straight through (D-18b). A non-finite request is unreadable, and an
    # unreadable shrink fails closed: no new risk this run.
    clamped = min(max(posture_mult, 0.0), 1.0) if math.isfinite(posture_mult) else 0.0
    modifiers = list(view.modifiers)
    if clamped < 1.0:
        modifiers.append(f"analyst posture ×{clamped:.2f}")
    return replace(view, risk_mult=view.risk_mult * clamped, modifiers=modifiers)


def breadth_pct(above_flags: list[bool]) -> float | None:
    return sum(above_flags) / len(above_flags) * 100 if above_flags else None


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def hours_until(moment: datetime, now: datetime | None = None) -> float:
    return (moment - (now or datetime.now(UTC))).total_seconds() / 3600
