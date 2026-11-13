"""Per-host token bucket (ARCHITECTURE.md §4).

One bucket per host, sized from the published limit of that host. Buckets are process-local,
which is right for ARGUS: every run is a short-lived process and only one run holds the lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Published free-tier limits (DATA_SOURCES.md §1–2), deliberately set below the real ceiling.
HOST_LIMITS: dict[str, tuple[float, float]] = {
    # host: (requests, per_seconds)
    "www.okx.com": (10, 2),
    "app.okx.com": (10, 2),
    "api.bybit.kz": (10, 1),
    "api.bybit.com": (10, 1),
    "fapi.binance.com": (20, 1),
    "api.coinalyze.net": (35, 60),  # free key is 40/min
    "api.hyperliquid.xyz": (10, 1),
    "www.deribit.com": (10, 1),
    "api.coingecko.com": (25, 60),  # demo key is 30/min
    "pro-api.coinmarketcap.com": (25, 60),
    "api.alternative.me": (10, 60),
    "api.llama.fi": (5, 1),
    "stablecoins.llama.fi": (5, 1),
    "mempool.space": (5, 1),
    "nfs.faireconomy.media": (1, 60),  # ForexFactory export throttles hard; cached 6h anyway
    "gamma-api.polymarket.com": (5, 1),
    "api.elections.kalshi.com": (5, 1),
    "news.google.com": (1, 2),
    "r.jina.ai": (10, 60),  # free reader tier
    "defillama-datasets.llama.fi": (5, 1),
    "efts.sec.gov": (5, 1),  # SEC fair-access limit is 10/s
    "data.sec.gov": (5, 1),
    # Free tier is 3 calls/s, 100k/day, selected chains only (docs.etherscan.io/rate-limits,
    # read 2026-10-05 — the 5/s figure in older notes is the paid Lite tier). Without this row
    # the default below would hand a future fetcher 5/s, i.e. above the ceiling.
    "api.etherscan.io": (2, 1),
}
DEFAULT_LIMIT = (5.0, 1.0)

# Endpoint families with their own, tighter limit than the host. OKX rubik statistics allow
# 5 requests per 2 s per IP; the host's (10, 2) admitted 20. (1, 0.6) spaces requests 0.6 s
# apart, so a 2 s window sees at most 4 as the client sends them. The fifth slot is headroom:
# spacing holds when the client wakes, not when OKX receives the request, and each call opens a
# fresh TLS handshake, so six could only land in one OKX window with over 1 s of jitter. At
# (1, 0.5) half a second was enough. A prefix here also gets its own host breaker (http.py).
PATH_LIMITS: dict[str, tuple[float, float]] = {
    "/api/v5/rubik/": (1, 0.6),
}


@dataclass
class TokenBucket:
    capacity: float
    refill_per_s: float
    tokens: float = field(init=False)
    updated: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def take(self, amount: float = 1.0, sleep: bool = True) -> float:
        """Reserve a token and wait until it is ours. Returns the seconds to wait.

        The token is taken under the lock *before* sleeping and the balance may go negative,
        so concurrent waiters queue behind each other (D-8). Taking it after the sleep let
        every blocked thread wake after one token's wait and go through together.
        `sleep=False` still reserves: the caller must wait the returned time itself.
        """
        with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_s)
            self.updated = now
            self.tokens -= amount
            wait = max(0.0, -self.tokens / self.refill_per_s)
        if sleep and wait > 0:
            time.sleep(wait)
        return wait


def bucket_key(url: str) -> str:
    """The host, or `host + prefix` for an endpoint family in `PATH_LIMITS`."""
    parsed = urlparse(url)
    host = parsed.netloc or url
    prefix = next((p for p in PATH_LIMITS if parsed.path.startswith(p)), "")
    return f"{host}{prefix}"


class RateLimiter:
    """Holds one bucket per host."""

    def __init__(self, limits: dict[str, tuple[float, float]] | None = None) -> None:
        self.limits = limits if limits is not None else HOST_LIMITS
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket_for(self, url: str) -> TokenBucket:
        key = bucket_key(url)
        host = urlparse(url).netloc or url
        limit = PATH_LIMITS.get(key[len(host) :], self.limits.get(host, DEFAULT_LIMIT))
        with self._lock:
            if key not in self._buckets:
                requests, per_seconds = limit
                self._buckets[key] = TokenBucket(
                    capacity=float(requests), refill_per_s=float(requests) / float(per_seconds)
                )
            return self._buckets[key]

    def acquire(self, url: str, sleep: bool = True) -> float:
        return self.bucket_for(url).take(sleep=sleep)


limiter = RateLimiter()
