"""Multi-source price consensus (DATA_SOURCES.md §2; FAILURE_LOG F-010).

On 11 Sep aggregators disagreed on BTC's price by ~2% *and on its direction*. The rule that
falls out: a trade-critical price needs a majority of at least three sources agreeing within
1%, and where they disagree, the venue we actually fill on wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from argus.data.quality import DataQualityEvent, check_price_consensus

# The venue we trade on outranks every aggregator; aggregators rank below other venues.
SOURCE_PRECEDENCE = ("okx", "bybit", "binance", "hyperliquid", "coingecko", "cmc")
DEFAULT_TOLERANCE_PCT = Decimal("1")


@dataclass
class ConsensusPrice:
    symbol: str
    price: Decimal | None
    chosen_source: str | None
    agreeing: list[str] = field(default_factory=list)
    outliers: dict[str, Decimal] = field(default_factory=dict)
    event: DataQualityEvent | None = None

    @property
    def trade_critical_ok(self) -> bool:
        """True only when enough independent sources agree to size a trade on it."""
        return self.price is not None and self.event is None and len(self.agreeing) >= 3


def _rank(source: str) -> int:
    for index, prefix in enumerate(SOURCE_PRECEDENCE):
        if source.startswith(prefix):
            return index
    return len(SOURCE_PRECEDENCE)


def consensus_price(
    symbol: str, prices: dict[str, Decimal], tolerance_pct: Decimal = DEFAULT_TOLERANCE_PCT
) -> ConsensusPrice:
    """Median-anchored agreement, resolved by source precedence."""
    usable = {name: price for name, price in prices.items() if price > 0}
    if not usable:
        return ConsensusPrice(
            symbol=symbol,
            price=None,
            chosen_source=None,
            event=DataQualityEvent(
                trap="trap_17_price_consensus",
                source="none",
                severity="reject",
                detail="no usable price from any source",
                symbol=symbol,
            ),
        )
    ordered = sorted(usable.values())
    median = ordered[len(ordered) // 2]
    agreeing = [
        name
        for name, price in usable.items()
        if abs(price - median) / median * Decimal("100") <= tolerance_pct
    ]
    outliers = {name: price for name, price in usable.items() if name not in agreeing}
    event: DataQualityEvent | None = None
    try:
        check_price_consensus(usable, symbol, tolerance_pct)
    except DataQualityEvent as fired:
        event = fired
    chosen = min(agreeing, key=_rank) if agreeing else min(usable, key=_rank)
    return ConsensusPrice(
        symbol=symbol,
        price=usable[chosen],
        chosen_source=chosen,
        agreeing=sorted(agreeing, key=_rank),
        outliers=outliers,
        event=event,
    )
