"""Review 2026-09-18 S-1, adapter half: the OKX adapter refuses to mutate in a forbidden quadrant.

`config.py` now refuses MODE=demo with OKX_DEMO=false, but the adapter itself held no refusal of
its own: the only sandbox decision was `set_sandbox_mode` at construction. Anything that built
Settings without validation (`model_construct`, a future code path, a test helper) could still
hand it the live key triple, and every order, amend, cancel and close would reach the real
account. The refusal now lives where the request is made, and nothing reaches the network.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from argus.config import Settings
from argus.exchange.okx import GuardedClient, MutationRefusedError, OKXAdapter

SYMBOL = "BTC/USDT:USDT"


class _Stub:
    """A ccxt stand-in that records every call that would have gone to OKX."""

    def __init__(self, sandbox: bool) -> None:
        self.headers: dict[str, str] = {"x-simulated-trading": "1"} if sandbox else {}
        self.calls: list[str] = []

    def private_post_trade_close_position(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("close_position")
        return {"data": [{"clOrdId": "x"}]}

    def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("create_order")
        return {"id": "1", "symbol": kwargs.get("symbol")}

    def cancel_order(self, order_id: str, symbol: str) -> None:
        self.calls.append("cancel_order")

    def private_get_account_balance(self) -> dict[str, Any]:
        self.calls.append("balance")
        return {"data": [{}]}


def _adapter(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, sandbox: bool
) -> tuple[OKXAdapter, _Stub]:
    adapter = OKXAdapter(settings)  # builds a real ccxt client; no request is made
    stub = _Stub(sandbox)
    adapter.client = GuardedClient(stub, adapter.mutation_allowed)
    monkeypatch.setattr(
        OKXAdapter,
        "_require_position",
        lambda _s, _sym: {"symbol": SYMBOL, "side": "long", "size": Decimal("1")},
    )
    monkeypatch.setattr(OKXAdapter, "_market", lambda _s, _sym: {"id": "BTC-USDT-SWAP"})
    return adapter, stub


def _forbidden() -> Settings:
    # The quadrant config.py refuses — built without validation, as a bypass would.
    return Settings.model_construct(mode="demo", okx_demo=False, live_trading="")


def test_the_fourth_quadrant_cannot_close_a_position(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, stub = _adapter(monkeypatch, _forbidden(), sandbox=False)
    with pytest.raises(MutationRefusedError):
        adapter.close(SYMBOL)
    with pytest.raises(MutationRefusedError):
        adapter.cancel_order(SYMBOL, "1")
    assert stub.calls == []


def test_reads_are_still_allowed_in_the_fourth_quadrant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing reads would blind the watchdog and `doctor`; only mutations are dangerous."""
    adapter, stub = _adapter(monkeypatch, _forbidden(), sandbox=False)
    adapter.client.private_get_account_balance()
    assert stub.calls == ["balance"]


def test_demo_without_the_sandbox_header_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """OKX_DEMO=true is a claim; the header on the client is what OKX actually sees."""
    adapter, stub = _adapter(monkeypatch, Settings(mode="demo", okx_demo=True), sandbox=False)
    with pytest.raises(MutationRefusedError):
        adapter.close(SYMBOL)
    assert stub.calls == []


def test_the_demo_venue_may_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, stub = _adapter(monkeypatch, Settings(mode="demo", okx_demo=True), sandbox=True)
    adapter.close(SYMBOL)
    assert stub.calls == ["close_position"]


def test_an_armed_live_account_may_mutate(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        mode="approve",
        okx_demo=False,
        live_trading="I_UNDERSTAND",
        live_venue="I_UNDERSTAND_REAL_MONEY",  # the separate live-money interlock (P-N5)
    )
    adapter, stub = _adapter(monkeypatch, settings, sandbox=False)
    adapter.close(SYMBOL)
    assert stub.calls == ["close_position"]


def test_a_real_demo_adapter_sets_the_header_the_guard_checks() -> None:
    assert OKXAdapter(Settings(mode="demo", okx_demo=True)).mutation_allowed()
    assert not OKXAdapter(_forbidden()).mutation_allowed()


def _offline_okx(sent: list[tuple[str, str]]) -> Any:
    """A real ccxt client whose HTTP layer records instead of sending."""
    import ccxt

    raw = ccxt.okx({"apiKey": "k", "secret": "s", "password": "p", "enableRateLimit": False})

    def fetch(url: str, method: str = "GET", headers: Any = None, body: Any = None) -> Any:
        sent.append((method, url))
        return {"code": "0", "data": [{}]}

    raw.fetch = fetch
    return raw


def test_every_spelling_of_a_mutation_is_refused() -> None:
    """Review 2026-10-07 (exec): only exact names and the snake_case `private_post` prefix were
    refused. `create_market_order` and the other create_* helpers, and the camelCase raw
    endpoints such as `privatePostTradeOrder`, passed the guard and reached the account."""
    sent: list[tuple[str, str]] = []
    client = GuardedClient(_offline_okx(sent), lambda: False)
    for name in (
        "create_market_order",
        "create_limit_order",
        "createOrder",
        "privatePostTradeOrder",
        "privatePostTradeAmendAlgos",
        "cancelAllOrders",
        "editOrder",
    ):
        with pytest.raises(MutationRefusedError):
            getattr(client, name)
    assert sent == []


def test_a_private_post_is_refused_at_the_request_itself() -> None:
    """Whatever the name, every call ends in one request; a private POST stops there."""
    sent: list[tuple[str, str]] = []
    client = GuardedClient(_offline_okx(sent), lambda: False)
    with pytest.raises(MutationRefusedError):
        client.request("trade/order", "private", "POST", {"instId": "BTC-USDT-SWAP"})
    assert sent == []
    client.private_get_account_balance()  # reads still pass
    assert [method for method, _ in sent] == ["GET"]


def test_an_armed_client_still_reaches_the_request_layer() -> None:
    sent: list[tuple[str, str]] = []
    client = GuardedClient(_offline_okx(sent), lambda: True)
    client.privatePostTradeOrder({"instId": "BTC-USDT-SWAP"})
    assert [method for method, _ in sent] == ["POST"]
