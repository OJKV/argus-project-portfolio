"""Journal versus exchange (ARCHITECTURE.md §8).

The exchange is truth. The journal is what we *believed*, and the gap between them is where
money goes missing quietly. Four names for four gaps:

* **ghost** — the journal has an open position the venue does not. Harmless-looking, and the
  reason a documented crypto bot accumulated 246 of them and stopped trading for months: every
  later run saw a position that was not there and refused to act.
* **orphan** — the venue has a position the journal cannot explain. Real money, no thesis, and
  no stop unless something attaches one.
* **unprotected** — a known position with no live stop. The one that costs the most, fastest.
* **size_drift** — same trade, different size, after a partial fill or a manual trim.

Pure: it compares two lists and returns what disagrees. Acting on the result is the watchdog's
job, so this can be reasoned about without a venue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Money at risk first, bookkeeping last. The order is the order an operator should read them in.
SEVERITY: dict[str, str] = {
    "unprotected": "critical",
    "orphan": "critical",
    "ghost": "warning",
    "size_drift": "warning",
}
_RANK = {"unprotected": 0, "orphan": 1, "size_drift": 2, "ghost": 3}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
OPEN_STATES = frozenset({"OPEN", "REDUCED", "PENDING_EXIT", "PENDING_ENTRY"})


def asset_of(symbol: str) -> str:
    """`BTC/USDT:USDT` and `BTC-USDT-SWAP` are one instrument; matching on the raw string would
    report every position as both a ghost and an orphan."""
    parts = _NON_ALNUM.sub(" ", str(symbol).strip().lower()).split()
    return parts[0] if parts else ""


def symbol_key(symbol: str) -> str:
    """One key for `BTC/USDT:USDT` (ccxt) and `BTC-USDT-SWAP` (OKX instId): base and quote.

    The fake adapter keyed reconcile on the raw symbol while the OKX adapter keyed on instId, so a
    spelling mismatch passed every fake-backed test (review P1-5). Both now go through here.
    """
    parts = _NON_ALNUM.sub(" ", str(symbol).strip().lower()).split()
    return "-".join(parts[:2])


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    symbol: str
    detail: str

    @property
    def severity(self) -> str:
        return SEVERITY.get(self.kind, "warning")


@dataclass
class Reconciliation:
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.discrepancies

    def of_kind(self, kind: str) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.kind == kind]


def _size(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def reconcile(
    journal_positions: list[dict[str, Any]],
    venue_positions: list[dict[str, Any]],
) -> Reconciliation:
    """Everything the two views disagree about, worst first."""
    ours = {
        (asset_of(p["symbol"]), p.get("side")): p
        for p in journal_positions
        if str(p.get("state", "OPEN")) in OPEN_STATES
    }
    theirs = {(asset_of(p["symbol"]), p.get("side")): p for p in venue_positions}
    found: list[Discrepancy] = []

    for key, venue in theirs.items():
        symbol = str(venue["symbol"])
        if not venue.get("protected", False):
            found.append(Discrepancy("unprotected", symbol, "no live stop on the exchange"))
        mine = ours.get(key)
        if mine is None:
            # Keyed on side as well as asset: a flipped position is not the same trade, and
            # treating it as drift would quietly adopt a position nobody planned.
            found.append(Discrepancy("orphan", symbol, "position has no plan in the journal"))
            continue
        if _size(mine.get("size")) != _size(venue.get("size")):
            found.append(
                Discrepancy(
                    "size_drift",
                    symbol,
                    f"journal {_size(mine.get('size'))} vs exchange {_size(venue.get('size'))}",
                )
            )

    for key, mine in ours.items():
        if key not in theirs:
            found.append(
                Discrepancy("ghost", str(mine["symbol"]), "journal is open, exchange is flat")
            )

    found.sort(key=lambda d: (_RANK.get(d.kind, 9), d.symbol))
    return Reconciliation(discrepancies=found)
