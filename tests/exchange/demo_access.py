"""Reaching the OKX demo venue from a test: skip for the environment, fail for the code.

Review 2026-09-18 X-16: both demo fixtures wrapped their first call in `except Exception:
pytest.skip(...)`, so a broken adapter — a parsing error, a KeyError, a refused response — read
as "demo unreachable" and the suite stayed green. Only the environment is a reason to skip: no
route (VPN off, geo-block), or a key this machine may not use (50110 IP allowlist, F-039; 50119
from a blocked edge, F-034). Everything else is a failure.
"""

from __future__ import annotations

from collections.abc import Callable

import ccxt
import pytest

ENVIRONMENTAL = (ccxt.NetworkError, ccxt.AuthenticationError)


def reach_demo[T](probe: Callable[[], T]) -> T:
    """Run the first venue call of a demo test; skip only if the environment is the problem."""
    try:
        return probe()
    except ENVIRONMENTAL as exc:
        pytest.skip(
            f"OKX demo unreachable from here ({type(exc).__name__}) — VPN off, or this "
            "machine's IP is not on the key's allowlist?"
        )
