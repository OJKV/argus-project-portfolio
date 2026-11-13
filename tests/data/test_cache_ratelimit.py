"""Cache keying/TTL and the per-host token bucket (ARCHITECTURE.md §4)."""

from __future__ import annotations

import threading
import time

import pytest

from argus.data.cache import cache_key
from argus.data.ratelimit import (
    DEFAULT_LIMIT,
    HOST_LIMITS,
    RateLimiter,
    TokenBucket,
)


def test_cache_key_is_stable_across_param_order() -> None:
    a = cache_key("okx.public.candles", {"instId": "BTC-USDT-SWAP", "bar": "1H"})
    b = cache_key("okx.public.candles", {"bar": "1H", "instId": "BTC-USDT-SWAP"})
    assert a == b


def test_cache_key_separates_sources_and_params() -> None:
    assert cache_key("a", {"x": 1}) != cache_key("b", {"x": 1})
    assert cache_key("a", {"x": 1}) != cache_key("a", {"x": 2})


def test_bucket_allows_a_burst_then_makes_the_caller_wait() -> None:
    bucket = TokenBucket(capacity=3, refill_per_s=1)
    assert [bucket.take(sleep=False) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert bucket.take(sleep=False) > 0  # fourth call must wait


def test_concurrent_waiters_are_spaced_not_released_together() -> None:
    """D-8: blocked threads did not reserve before sleeping, so every one of them woke after a
    single token's wait and went through together — a 429 burst on OKX with ~60 threads."""
    bucket = TokenBucket(capacity=1, refill_per_s=20)  # one token per 50 ms
    start = time.monotonic()
    threads = [threading.Thread(target=bucket.take) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # One token up front, then five more at 50 ms each: at least ~250 ms for all six.
    assert time.monotonic() - start >= 0.22


def test_a_waiter_does_not_forgive_its_own_debt() -> None:
    bucket = TokenBucket(capacity=2, refill_per_s=10)
    waits = [bucket.take(sleep=False) for _ in range(5)]
    # Each reservation queues behind the previous one: 0, 0, 0.1, 0.2, 0.3 s.
    assert waits[:2] == [0.0, 0.0]
    assert waits[2] < waits[3] < waits[4]
    assert waits[4] == pytest.approx(0.3, abs=0.02)


def test_okx_rubik_has_its_own_tighter_bucket() -> None:
    """OKX rubik statistics allow 5 requests per 2 s per IP, half the market-data rate; sharing
    the host bucket let rubik bursts through at 10/2 s (D-8)."""
    limiter = RateLimiter()
    host = limiter.bucket_for("https://www.okx.com/api/v5/market/tickers")
    rubik = limiter.bucket_for("https://www.okx.com/api/v5/rubik/stat/taker-volume")
    assert rubik is not host
    assert rubik is limiter.bucket_for("https://www.okx.com/api/v5/rubik/stat/contracts/x")
    # Most requests any 2 s window can see: the burst plus the refill.
    assert rubik.capacity + rubik.refill_per_s * 2 <= 5


def test_the_rubik_bucket_leaves_headroom_for_network_jitter() -> None:
    """Spacing is enforced when the client wakes, not when OKX receives the request. At exactly
    5 per 2 s, half a second of latency jitter (every call opens a fresh TLS handshake, over
    the VPN) can land six inside OKX's window, and each 429 counted against the breaker."""
    rubik = RateLimiter().bucket_for("https://www.okx.com/api/v5/rubik/stat/taker-volume")
    wakes = [rubik.take(sleep=False) for _ in range(20)]
    jitter = 0.5
    worst = max(sum(1 for w in wakes if start <= w <= start + 2 + jitter) for start in wakes)
    assert worst <= 5


def test_each_host_gets_its_own_bucket() -> None:
    limiter = RateLimiter()
    okx = limiter.bucket_for("https://www.okx.com/api/v5/market/tickers")
    binance = limiter.bucket_for("https://fapi.binance.com/fapi/v1/klines")
    assert okx is not binance
    assert okx is limiter.bucket_for("https://www.okx.com/api/v5/public/open-interest")


def test_unknown_host_falls_back_to_the_conservative_default() -> None:
    bucket = RateLimiter().bucket_for("https://unknown.example.com/x")
    assert bucket.capacity == DEFAULT_LIMIT[0]


def test_etherscan_is_capped_below_its_free_tier_ceiling() -> None:
    """Etherscan's free tier is 3 calls/s; the generic default (5/s) would exceed it.

    Written when Etherscan announced daily-cap enforcement (2026-11-01) with paid overage.
    No fetcher calls the host yet, so this guards the first one that does: an unlisted host
    silently inherits DEFAULT_LIMIT, which here would be a quota breach, not a slow fetch.
    """
    assert "api.etherscan.io" in HOST_LIMITS
    requests, per_seconds = HOST_LIMITS["api.etherscan.io"]
    assert requests / per_seconds <= 3.0
    assert (requests, per_seconds) != DEFAULT_LIMIT
