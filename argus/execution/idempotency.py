"""Deterministic client order ids (ARCHITECTURE.md §8).

The dangerous case is a retry after a timeout: the request may have reached OKX while the reply
was lost, so the second attempt must carry the *same* id or a network hiccup becomes two
positions. The id is therefore derived from the intention — this run, this instrument, this
direction — and never from a clock or a counter.

OKX allows 1..32 characters, letters and digits only.
"""

from __future__ import annotations

import hashlib
import re

PREFIX = "argus"
MAX_LEN = 32
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _canonical(symbol: str) -> str:
    """`BTC/USDT:USDT` and `BTC-USDT-SWAP` are the same instrument to us.

    ccxt returns the first spelling and OKX the second; if they hashed differently, a
    reconcile-then-retry would fail to recognise its own order.
    """
    base = _NON_ALNUM.sub(" ", symbol.strip().lower()).split()
    return base[0] if base else ""


def client_order_id(run_id: str, symbol: str, side: str) -> str:
    """A stable id for one intended entry. Same inputs, same id, forever."""
    seed = f"{run_id.strip()}|{_canonical(symbol)}|{side.strip().lower()}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{PREFIX}{digest}"[:MAX_LEN]
