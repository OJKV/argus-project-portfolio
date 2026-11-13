"""FakeExchange behaviours the gate and watchdog rely on."""

from __future__ import annotations

from decimal import Decimal

import pytest

from argus.exchange.fake import FakeExchange

BTC = "BTC-USDT-SWAP"


def _bracket(cl_ord_id: str = "run00001plan0001") -> dict[str, object]:
    return {
        "symbol": BTC,
        "side": "long",
        "cl_ord_id": cl_ord_id,
        "size": Decimal("1"),
        "price": Decimal("75000"),
        "attach_algos": [
            {"kind": "sl", "trigger_px": Decimal("72000")},
            {"kind": "tp", "trigger_px": Decimal("80000"), "size": Decimal("0.5")},
        ],
    }


def test_bracket_creates_entry_plus_live_protection() -> None:
    ex = FakeExchange()
    ids = ex.place_bracket(_bracket())
    assert "entry_id" in ids and "sl_algo_id" in ids
    assert [a["kind"] for a in ex.fetch_algo_orders()] == ["sl", "tp"]


def test_same_client_order_id_never_double_places() -> None:
    ex = FakeExchange()
    first = ex.place_bracket(_bracket())
    second = ex.place_bracket(_bracket())
    assert first["entry_id"] == second["entry_id"]
    assert len(ex.orders) == 1


def test_net_mode_refuses_an_opposite_position_on_the_same_symbol() -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC, side="long")
    with pytest.raises(ValueError, match="net mode"):
        # A coherent short: stop ABOVE the entry. The old fixture flipped only the side and kept
        # the long's stop below, which the fake now rejects first — correctly, and for a
        # different reason than this test is about.
        ex.place_bracket(
            {
                **_bracket("x2"),
                "side": "short",
                "attach_algos": [{"kind": "sl", "trigger_px": Decimal("78000")}],
            }
        )


def test_reconcile_flags_a_position_with_no_stop() -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    assert ex.reconcile()["unprotected"] == [BTC]
    ex.amend_stop(BTC, Decimal("70000"))
    assert ex.reconcile()["unprotected"] == []


def test_reconcile_speaks_the_okx_adapter_contract() -> None:
    """The fake returned an `orphans` key the real adapter never had (review X-16).

    Orphan positions are now judged against the journal by the watchdog (X-N2).
    """
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    assert set(ex.reconcile()) == {
        "positions",
        "unprotected",
        "open_orders",
        "algo_orders",
        "orphan_algos",
    }


def test_a_trailing_stop_live_from_placement_counts_as_protection() -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    ex.place_trailing(BTC, callback_pct=Decimal("2"), activation=None)
    assert ex.reconcile()["unprotected"] == []


def test_a_trailing_stop_waiting_for_activation_is_not_protection() -> None:
    """Review X-9: before it activates it protects nothing (this test used to assert the
    opposite)."""
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    ex.place_trailing(BTC, callback_pct=Decimal("2"), activation=Decimal("77000"))
    assert ex.reconcile()["unprotected"] == [BTC]


def test_zero_quantity_fill_produces_no_position() -> None:
    # F-030: 537 division-by-zero errors came from trusting a zero-qty fill.
    ex = FakeExchange(zero_qty_fill=True)
    ids = ex.place_bracket(_bracket())
    fill = ex.fill(str(ids["entry_id"]))
    assert fill["size"] == 0
    assert ex.fetch_positions() == []


def test_fee_rates_are_negative_as_okx_reports_charges() -> None:
    # F-036: a positive taker rate would invert every fee-viability calculation.
    fees = FakeExchange().fetch_fee_rates(BTC)
    assert fees["taker"] < 0 and fees["maker"] < 0


def test_leverage_beyond_the_instrument_maximum_is_refused() -> None:
    ex = FakeExchange()
    with pytest.raises(ValueError, match="outside 1..100"):
        ex.set_leverage(BTC, 150)


def test_injected_failure_surfaces_once_then_clears() -> None:
    ex = FakeExchange(fail_next=TimeoutError("venue down"))
    with pytest.raises(TimeoutError):
        ex.fetch_positions()
    assert ex.fetch_positions() == []


def test_close_cancels_the_attached_algos() -> None:
    ex = FakeExchange()
    ids = ex.place_bracket(_bracket())
    ex.fill(str(ids["entry_id"]))
    ex.close(BTC)
    assert ex.fetch_positions() == []
    assert ex.fetch_algo_orders() == []


def test_partial_close_reduces_size_and_keeps_the_position() -> None:
    ex = FakeExchange()
    ids = ex.place_bracket({**_bracket(), "size": Decimal("2")})
    ex.fill(str(ids["entry_id"]))
    ex.partial_close(BTC, 50)
    assert ex.fetch_positions()[0]["size"] == Decimal("1")
