"""Fee and funding viability — gate 13 (DECISION_LOGIC.md §6).

A trade that cannot pay for itself is not a trade. Rates always come from the account endpoint
(FAILURE_LOG F-026: real rates were 6–12× the documented ones); OKX reports charges as negative
numbers (F-036), so a positive taker rate means the convention changed and is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

MAX_COST_SHARE_OF_TP1 = 0.20  # gate 13
FUNDING_PERIOD_HOURS = 8


@dataclass(frozen=True)
class FeeRates:
    maker: Decimal
    taker: Decimal


def expected_costs(
    notional: float,
    rates: FeeRates,
    entry_is_maker: bool,
    funding_8h: float,
    hours: float,
    side: Literal["long", "short"] = "long",
    interval_hours: float = FUNDING_PERIOD_HOURS,
) -> dict[str, float]:
    """Round-trip fees plus funding over the expected hold, in quote currency.

    `funding_8h` is the venue's rate PER FUNDING PERIOD, and `interval_hours` that period: OKX
    settles many alts every 4, 2 or 1 h, where assuming 8 h understated funding 2-8x (G-9).
    """
    if not interval_hours > 0:  # also catches NaN
        raise ValueError(f"funding interval_hours must be positive, got {interval_hours!r}")
    if rates.taker > 0 or rates.maker > 0:
        raise ValueError(
            f"fee rate is positive — OKX reports charges as negative: "
            f"maker={rates.maker} taker={rates.taker}"
        )
    entry_rate = abs(float(rates.maker if entry_is_maker else rates.taker))
    exit_rate = abs(float(rates.taker))  # exits are stops and market closes
    fees = notional * (entry_rate + exit_rate)
    periods = max(0.0, hours) / interval_hours
    funding = notional * funding_8h * periods * (1 if side == "long" else -1)
    # Funding is a cost or nothing (review G-8): fees are certain, a projected funding credit is
    # not — the rate flips. Netting the credit let a 22 %-of-TP1 fee load pass as negative.
    return {"fees": fees, "funding": funding, "total": fees + max(0.0, funding)}


def fee_viable(
    notional: float,
    expected_tp1_profit: float,
    rates: FeeRates,
    entry_is_maker: bool,
    funding_8h: float,
    hours: float,
    side: Literal["long", "short"] = "long",
    limit: float = MAX_COST_SHARE_OF_TP1,
    interval_hours: float = FUNDING_PERIOD_HOURS,
) -> tuple[bool, str]:
    costs = expected_costs(notional, rates, entry_is_maker, funding_8h, hours, side, interval_hours)
    if expected_tp1_profit <= 0:
        return False, "no expected profit at TP1"
    share = costs["total"] / expected_tp1_profit
    detail = (
        f"costs ${costs['total']:.2f} (fees ${costs['fees']:.2f}, funding ${costs['funding']:.2f}) "
        f"= {share * 100:.1f}% of ${expected_tp1_profit:.2f} TP1 profit, limit {limit * 100:.0f}%"
    )
    return share <= limit, detail
