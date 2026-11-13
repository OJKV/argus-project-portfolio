"""The live account starts flat, and `argus doctor --live-keys` says whether it is (review
2026-09-18 P-10, PARTIAL on 2026-10-07).

ARGUS reconciles whatever it finds on its account as its own: a position the owner opened by hand
on the live sub-account before the flip would be managed — stop-amended, trimmed, flattened by the
kill-switch — by a system that never planned it, and counted in its risk as if it had. The docs
promised a starts-flat check; there was none, and GO_LIVE_CHECKLIST could only say "check the OKX
app". The weekly live-key read now counts positions, open orders and algo orders on the live
account and fails while the box is not yet armed on it. Once armed on live, they are expected.
Offline: ccxt is replaced by a fake; nothing reaches OKX.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from argus import config, doctor

ARMED_LIVE = {
    "MODE": "approve",
    "LIVE_TRADING": "I_UNDERSTAND",
    "OKX_DEMO": "false",
    "LIVE_VENUE": "I_UNDERSTAND_REAL_MONEY",
}


class _FakeOkx:
    positions: ClassVar[list[dict[str, Any]]] = []
    orders: ClassVar[list[dict[str, Any]]] = []
    algos: ClassVar[dict[str, list[dict[str, Any]]]] = {}
    calls: ClassVar[list[str]] = []

    def __init__(self, _cfg: dict[str, Any]) -> None:
        pass

    def set_sandbox_mode(self, _on: bool) -> None:
        pass

    def private_get_account_config(self) -> dict[str, Any]:
        row = {"acctLv": "2", "posMode": "net_mode", "perm": "read_only,trade", "ip": "x"}
        return {"data": [row]}

    def private_get_account_balance(self) -> dict[str, Any]:
        raise RuntimeError("not needed here")

    def private_get_account_trade_fee(self, _params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("not needed here")

    def private_get_account_positions(self, _params: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append("positions")
        return {"data": type(self).positions}

    def private_get_trade_orders_pending(self, _params: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append("orders")
        return {"data": type(self).orders}

    def private_get_trade_orders_algo_pending(self, params: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append(f"algo:{params['ordType']}")
        return {"data": type(self).algos.get(params["ordType"], [])}


@pytest.fixture
def okx(monkeypatch: pytest.MonkeyPatch) -> type[_FakeOkx]:
    for env in ("DEMO_API", "API"):
        for name in ("KEY", "SECRET", "PASSPHRASE"):
            monkeypatch.setenv(f"OKX_{env}_{name}", "x")
    _FakeOkx.positions, _FakeOkx.orders, _FakeOkx.algos, _FakeOkx.calls = [], [], {}, []
    monkeypatch.setitem(sys.modules, "ccxt", SimpleNamespace(okx=_FakeOkx))
    config.get_settings.cache_clear()
    return _FakeOkx


def _flat_row(env: str = "live") -> doctor.Check:
    rows = {c.name: c for c in doctor.check_okx_auth(env=env)}
    return rows[f"okx.{env}.flat"]


def test_a_flat_live_account_passes(okx: type[_FakeOkx]) -> None:
    row = _flat_row()
    assert row.ok
    assert "flat" in row.detail
    assert {"positions", "orders", "algo:conditional", "algo:oco"} <= set(okx.calls)


def test_a_position_before_the_flip_fails(okx: type[_FakeOkx]) -> None:
    okx.positions = [{"instId": "BTC-USDT-SWAP", "pos": "0.01"}]
    row = _flat_row()
    assert not row.ok
    assert "1 position" in row.detail
    assert "safety review" in row.detail


def test_a_zero_size_row_is_not_a_position(okx: type[_FakeOkx]) -> None:
    okx.positions = [{"instId": "BTC-USDT-SWAP", "pos": "0"}]
    assert _flat_row().ok


def test_resting_and_algo_orders_count(okx: type[_FakeOkx]) -> None:
    okx.orders = [{"instId": "ETH-USDT-SWAP"}]
    okx.algos = {"conditional": [{"instId": "ETH-USDT-SWAP"}]}
    row = _flat_row()
    assert not row.ok
    assert "1 open order" in row.detail and "1 algo order" in row.detail


def test_armed_on_live_positions_are_expected(
    okx: type[_FakeOkx], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in ARMED_LIVE.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    okx.positions = [{"instId": "BTC-USDT-SWAP", "pos": "-0.01"}]
    row = _flat_row()
    assert row.ok
    assert "armed on live" in row.detail


def test_the_demo_account_is_not_held_to_flat(okx: type[_FakeOkx]) -> None:
    okx.positions = [{"instId": "BTC-USDT-SWAP", "pos": "0.01"}]
    rows = {c.name for c in doctor.check_okx_auth(env="demo")}
    assert "okx.demo.flat" not in rows


def test_a_failed_read_fails_the_row_without_echoing_a_key(
    okx: type[_FakeOkx], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_self: Any, _params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("okx 50110 apiKey=x")

    monkeypatch.setattr(_FakeOkx, "private_get_account_positions", boom)
    row = _flat_row()
    assert not row.ok
    assert "apiKey" not in row.detail
