"""Per-dependency circuit breakers (ARCHITECTURE.md §8, §14; FAILURE_LOG F-029).

closed → open after `threshold` failures inside `window_s` → half_open after `cooldown_s`
→ closed on the next success, back to open on the next failure. Half-open admits ONE probe
(review X-19): every caller used to pass, so the first instant after a cooldown sent a burst at
a dependency that had just been failing. A probe that reports nothing within another
`cooldown_s` (its caller skipped the call) frees the slot for the next.

Two rules that matter more than the state machine:
  * cooldowns key on **attempts**, not successes — counting successes let three failed buys
    through in minutes (F-029);
  * an open breaker blocks new *entries* but never an emergency stop repair. A venue that is
    flaky is exactly when a position is most likely to be unprotected, so `allow(force=True)`
    exists and the watchdog is the only caller.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Literal

State = Literal["closed", "open", "half_open"]


@dataclass
class CircuitBreaker:
    name: str
    threshold: int = 5
    window_s: float = 60.0
    cooldown_s: float = 30.0
    state: State = "closed"
    attempts: int = 0
    _failures: list[float] = field(default_factory=list)
    _opened_at: float | None = None
    _probe_at: float | None = None
    _clock: object = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def allow(self, force: bool = False) -> bool:
        """May we call this dependency now? `force` is for stop-loss repair only."""
        with self._lock:
            self.attempts += 1
            if force:
                return True
            now = self._now()
            if self.state == "open":
                assert self._opened_at is not None
                if now - self._opened_at < self.cooldown_s:
                    return False
                self.state = "half_open"
            elif self.state == "half_open":
                if self._probe_at is not None and now - self._probe_at < self.cooldown_s:
                    return False
            else:
                return True
            self._probe_at = now
            return True

    def would_allow(self) -> bool:
        """What `allow()` would answer, without taking the half-open probe slot.

        For listings and availability filters that may never make the call: a peek must not
        use up the one probe a recovering dependency gets.
        """
        with self._lock:
            now = self._now()
            if self.state == "open":
                assert self._opened_at is not None
                return now - self._opened_at >= self.cooldown_s
            if self.state == "half_open":
                return self._probe_at is None or now - self._probe_at >= self.cooldown_s
            return True

    def record_success(self) -> State:
        with self._lock:
            self._failures.clear()
            self.state = "closed"
            self._opened_at = self._probe_at = None
            return self.state

    def record_failure(self) -> State:
        with self._lock:
            return self._record_failure_locked()

    def _record_failure_locked(self) -> State:
        now = self._now()
        self._failures = [t for t in self._failures if now - t < self.window_s]
        self._failures.append(now)
        if self.state == "half_open" or len(self._failures) >= self.threshold:
            self.state = "open"
            self._opened_at = now
            self._probe_at = None
        return self.state


@dataclass
class BreakerRegistry:
    """One breaker per dependency name, created on first use. Safe to share across threads."""

    threshold: int = 5
    window_s: float = 60.0
    cooldown_s: float = 30.0
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            return self._get_locked(name)

    def _get_locked(self, name: str) -> CircuitBreaker:
        if name not in self.breakers:
            self.breakers[name] = CircuitBreaker(
                name, threshold=self.threshold, window_s=self.window_s, cooldown_s=self.cooldown_s
            )
        return self.breakers[name]

    def open_dependencies(self) -> list[str]:
        # Under the lock: `get` inserts into the dict this iterates (review X-19).
        with self._lock:
            return [n for n, b in self.breakers.items() if b.state == "open"]
