"""Review 2026-09-18 X-16: the demo tests skipped on *any* exception.

`except Exception: pytest.skip("OKX demo unreachable")` meant a broken adapter — a parsing error,
a KeyError, a refused response — showed up as a grey skip instead of a red failure. Only the
environment (no route, VPN off, key not allowlisted for this machine) is a reason to skip.
"""

from __future__ import annotations

import ccxt
import pytest

from argus.exchange.okx import ExchangeResponseError
from tests.exchange.demo_access import reach_demo


def _raise(exc: Exception) -> object:
    raise exc


@pytest.mark.parametrize(
    "exc",
    [
        ccxt.NetworkError("connection refused"),
        ccxt.RequestTimeout("timed out"),
        ccxt.PermissionDenied('okx {"code":"50110"}'),
        ccxt.AuthenticationError('okx {"code":"50119"}'),
    ],
)
def test_an_unreachable_environment_skips(exc: Exception) -> None:
    with pytest.raises(pytest.skip.Exception):
        reach_demo(lambda: _raise(exc))


@pytest.mark.parametrize(
    "exc",
    [
        ExchangeResponseError("balance response has no USDT row"),
        KeyError("eq"),
        ccxt.BadRequest('okx {"code":"51000"}'),
    ],
)
def test_a_broken_adapter_fails_instead_of_skipping(exc: Exception) -> None:
    with pytest.raises(type(exc)):
        reach_demo(lambda: _raise(exc))


def test_a_working_probe_returns_its_value() -> None:
    assert reach_demo(lambda: 42) == 42
