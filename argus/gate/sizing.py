"""Position sizing and leverage — DECISION_LOGIC.md §5 and §5.1 (small-account steps).

Pure functions, no I/O. The LLM never touches these inputs; the Risk Gate calls `size_position`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

Side = Literal["long", "short"]

# Below this notional fees and ticks dominate, so small-account steps escalate [tune].
MIN_VIABLE_NOTIONAL_USD = 25.0


@dataclass(frozen=True)
class InstrumentSpec:
    contract_value: Decimal  # base units per contract (OKX ctVal), e.g. BTC 0.01
    lot_size: Decimal  # contracts step (OKX lotSz)
    min_contracts: Decimal  # OKX minSz
    max_leverage: int
    maint_margin_rate: Decimal  # tier-1 mmr, e.g. 0.005
    taker_fee: Decimal = Decimal("0.0005")


@dataclass(frozen=True)
class StepPolicy:
    step: int
    risk_pct: float
    max_lev: int
    min_lev: int
    max_notional_frac: float


@dataclass(frozen=True)
class SizingInput:
    equity_usd: float
    entry: float
    stop: float
    side: Side
    mark: float
    atr_pct: float  # ATR14 / price on the plan timeframe, in percent
    regime_mult: float
    protection_mult: float
    spec: InstrumentSpec
    steps: tuple[StepPolicy, ...]
    # The adversarial chain's factor (Risk Debate). Clamped where it is used, so an agent can
    # only ever shrink what the gate already decided — no value here can mean "more".
    agent_mult: float = 1.0
    liq_buffer_multiple: float = 2.5
    stop_atr_band: tuple[float, float] = (1.0, 3.5)
    min_notional_buffer: float = 1.2
    min_viable_notional_usd: float = MIN_VIABLE_NOTIONAL_USD
    fee_buffer: float = 0.002  # extra distance reserved for fees/slippage in liq calc


@dataclass(frozen=True)
class SizingResult:
    ok: bool
    reason: str
    step: int
    risk_usd: float
    stop_dist_pct: float
    notional_usd: float
    leverage: int
    margin_usd: float
    contracts: Decimal
    liq_dist_pct: float


def stop_distance_pct(entry: float, stop: float, side: Side) -> float:
    if entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be positive")
    if side == "long" and stop >= entry:
        raise ValueError("long stop must be below entry")
    if side == "short" and stop <= entry:
        raise ValueError("short stop must be above entry")
    return abs(entry - stop) / entry


def liquidation_distance_pct(leverage: int, mmr: Decimal, fee_buffer: float) -> float:
    """Approximate isolated-margin liquidation distance as a fraction of entry price."""
    return (1.0 / leverage) - float(mmr) - fee_buffer


def leverage_for(
    notional: float,
    margin_budget: float,
    stop_dist: float,
    spec: InstrumentSpec,
    step: StepPolicy,
    liq_buffer_multiple: float,
    fee_buffer: float,
) -> int | None:
    """Smallest leverage that fits the margin budget while keeping liquidation >= k x stop."""
    if margin_budget <= 0:
        return None
    lev = max(step.min_lev, math.ceil(notional / margin_budget))
    lev = min(lev, step.max_lev, spec.max_leverage)
    while lev >= step.min_lev:
        if (
            liquidation_distance_pct(lev, spec.maint_margin_rate, fee_buffer)
            >= liq_buffer_multiple * stop_dist
        ):
            return lev
        lev -= 1
    return None


def contracts_for(notional: float, mark: float, spec: InstrumentSpec) -> Decimal:
    raw = Decimal(str(notional)) / (spec.contract_value * Decimal(str(mark)))
    return (raw / spec.lot_size).to_integral_value(rounding=ROUND_DOWN) * spec.lot_size


def size_position(inp: SizingInput) -> SizingResult:
    stop_dist = stop_distance_pct(inp.entry, inp.stop, inp.side)
    lo, hi = inp.stop_atr_band
    atr = inp.atr_pct / 100.0
    if not math.isfinite(atr) or atr <= 0:
        return _fail("BAD_ATR", stop_dist)
    if not (lo * atr <= stop_dist <= hi * atr):
        return _fail("STOP_OUTSIDE_ATR_BAND", stop_dist)

    min_notional = max(
        float(spec_min_notional(inp.spec, inp.mark)) * inp.min_notional_buffer,
        inp.min_viable_notional_usd,
    )
    agent_mult = min(1.0, max(0.0, inp.agent_mult))
    reduction = inp.regime_mult * inp.protection_mult * agent_mult
    for step in inp.steps:
        # The step is chosen on the UNREDUCED trade (review G-1 / G-N2). Escalation exists because
        # a small account's 1 % trade is too small to place at all; it must not also be the way a
        # reduced trade buys its size back. Before this, $150 with a halved trade fell under the
        # floor at step 0, escalated, and placed the identical order the reduction was meant to
        # shrink — regime, post-loss, Red Team and Risk Debate reductions all bought nothing.
        base_risk = inp.equity_usd * step.risk_pct / 100.0
        margin_budget = inp.equity_usd * step.max_notional_frac
        full_notional = min(base_risk / stop_dist, margin_budget)
        if full_notional < min_notional:
            continue  # escalate to the next small-account step
        full_lev = leverage_for(
            full_notional,
            margin_budget,
            stop_dist,
            inp.spec,
            step,
            inp.liq_buffer_multiple,
            inp.fee_buffer,
        )
        if full_lev is None or contracts_for(full_notional, inp.mark, inp.spec) < (
            inp.spec.min_contracts
        ):
            continue

        # This is the step. The reductions apply inside it, and a trade they shrink below what
        # can be placed is refused here rather than escalated into a bigger one.
        risk_usd = base_risk * reduction
        notional = min(risk_usd / stop_dist, margin_budget * inp.regime_mult)
        if notional < min_notional:
            return _fail("REDUCED_BELOW_MINIMUM", stop_dist)
        lev = leverage_for(
            notional,
            margin_budget,
            stop_dist,
            inp.spec,
            step,
            inp.liq_buffer_multiple,
            inp.fee_buffer,
        )
        contracts = contracts_for(notional, inp.mark, inp.spec)
        if lev is None or contracts < inp.spec.min_contracts:
            return _fail("REDUCED_BELOW_MINIMUM", stop_dist)
        notional_actual = float(contracts * inp.spec.contract_value * Decimal(str(inp.mark)))
        margin = notional_actual / lev
        liq = liquidation_distance_pct(lev, inp.spec.maint_margin_rate, inp.fee_buffer)
        # What the placed contracts lose at the stop (review G-11), not the budget: the cap and
        # lot rounding only ever shrink the order, so the budget overstated risk — the heat gate
        # blocked on risk that did not exist, and rounding of that fiction decided verdicts at
        # the edge of the 5 % cap (G-20). Unrounded, for the same reason.
        placed_risk = float(
            contracts
            * inp.spec.contract_value
            * abs(Decimal(str(inp.entry)) - Decimal(str(inp.stop)))
        )
        return SizingResult(
            True,
            "OK",
            step.step,
            placed_risk,
            stop_dist,
            round(notional_actual, 2),
            lev,
            round(margin, 2),
            contracts,
            liq,
        )
    return _fail("INSUFFICIENT_BALANCE", stop_dist)


def spec_min_notional(spec: InstrumentSpec, mark: float) -> Decimal:
    return spec.min_contracts * spec.contract_value * Decimal(str(mark))


def _fail(reason: str, stop_dist: float) -> SizingResult:
    return SizingResult(False, reason, -1, 0.0, stop_dist, 0.0, 0, 0.0, Decimal(0), 0.0)
