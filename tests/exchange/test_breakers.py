"""Circuit breakers and the watchdog's kill-switch numbers (review 2026-09-18, X-19).

* `half_open` admitted every caller, so the first moment after the cooldown let a burst through
  to a dependency that had just been failing. It admits one probe; the probe's result closes or
  reopens the breaker, and a probe that never reports cannot wedge it shut.
* `open_dependencies` iterated the registry without its lock while `get` could insert.
* The watchdog kept its own copies of the 15% drawdown kill and the 200% margin floor beside
  `gate/killswitch.py`'s — two numbers that could drift apart while one test kept them equal.
"""

from __future__ import annotations

import subprocess
import sys
import threading

from argus.exchange.breakers import BreakerRegistry, CircuitBreaker


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _tripped(clock: _Clock) -> CircuitBreaker:
    breaker = CircuitBreaker("venue", threshold=1, window_s=60.0, cooldown_s=30.0, _clock=clock)
    breaker.record_failure()
    assert breaker.state == "open"
    clock.now += 30.0
    return breaker


def test_half_open_admits_a_single_probe() -> None:
    clock = _Clock()
    breaker = _tripped(clock)
    assert breaker.allow() is True, "the probe"
    assert breaker.state == "half_open"
    assert breaker.allow() is False, "a second caller while the probe is out"
    assert breaker.allow() is False


def test_a_successful_probe_closes_the_breaker() -> None:
    clock = _Clock()
    breaker = _tripped(clock)
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() and breaker.allow()


def test_a_failed_probe_reopens_for_a_full_cooldown() -> None:
    clock = _Clock()
    breaker = _tripped(clock)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow() is False
    clock.now += 30.0
    assert breaker.allow() is True
    assert breaker.allow() is False


def test_a_probe_that_never_reports_does_not_wedge_the_breaker() -> None:
    """A caller that took the probe and then made no call (a cache hit, a rate-limit skip)."""
    clock = _Clock()
    breaker = _tripped(clock)
    assert breaker.allow()
    clock.now += 29.0
    assert breaker.allow() is False
    clock.now += 1.0
    assert breaker.allow() is True, "another probe once the first has had a cooldown to report"


def test_force_still_bypasses_a_half_open_breaker() -> None:
    """The watchdog's stop repair goes through whatever state the breaker is in."""
    clock = _Clock()
    breaker = _tripped(clock)
    assert breaker.allow()
    assert breaker.allow(force=True) is True


def test_open_dependencies_reads_the_registry_under_its_lock() -> None:
    registry = BreakerRegistry(threshold=1)
    registry.get("okx").record_failure()
    result: list[list[str]] = []
    reader = threading.Thread(target=lambda: result.append(registry.open_dependencies()))
    with registry._lock:  # a `get` inserting a new breaker holds this
        reader.start()
        reader.join(timeout=0.2)
        assert reader.is_alive(), "open_dependencies iterated while the registry was locked"
    reader.join(timeout=5)
    assert result == [["okx"]]


def test_the_watchdog_reads_the_kill_switch_numbers_from_the_gate() -> None:
    """Changed in `gate/killswitch.py`, the watchdog follows — there is one number, not two."""
    script = (
        "import argus.gate.killswitch as k\n"
        "k.PORTFOLIO_DD_LIMIT_PCT = 12.5\n"
        "k.MARGIN_RATIO_FLOOR_PCT = 150.0\n"
        "import argus.runs.watchdog as w\n"
        "print(w.DD_HALT_PCT, w.MARGIN_RATIO_FLOOR)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.split() == ["12.5", "150"]


def test_the_watchdog_numbers_render_as_before() -> None:
    """The dashboard prints `halts at 15%`; a Decimal('15.0') would print `15.0%`."""
    from argus.runs.watchdog import DD_HALT_PCT, MARGIN_RATIO_FLOOR

    assert (str(DD_HALT_PCT), str(MARGIN_RATIO_FLOOR)) == ("15", "200")


def test_would_allow_peeks_without_taking_the_probe_slot() -> None:
    """A listing is not a call: peeking twice must leave the one probe for the real request."""
    clock = _Clock()
    breaker = _tripped(clock)
    assert breaker.would_allow() is True
    assert breaker.would_allow() is True
    assert breaker.allow() is True, "the probe was consumed by a peek"
    assert breaker.would_allow() is False, "the probe is out now"
