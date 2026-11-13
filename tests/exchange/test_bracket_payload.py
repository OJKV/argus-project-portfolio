"""OKX bracket payload shape — pure function, no network (ARCHITECTURE.md §8, F-003)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from argus.exchange.okx import build_bracket_params


def _order(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "symbol": "BTC/USDT:USDT",
        "side": "long",
        "cl_ord_id": "a1b2c3d4e5f6a7b8",
        "size": Decimal("1"),
        "price": Decimal("75000"),
        "attach_algos": [
            {"kind": "sl", "trigger_px": Decimal("72000")},
            {"kind": "tp", "trigger_px": Decimal("80000"), "size": Decimal("0.5")},
        ],
    }
    base.update(over)
    return base


def test_stop_is_attached_at_placement_with_mark_trigger_and_market_exit() -> None:
    params = build_bracket_params(_order())
    sl = next(a for a in params["attachAlgoOrds"] if "slTriggerPx" in a)
    assert sl["slTriggerPx"] == "72000"
    assert sl["slOrdPx"] == "-1"  # market on trigger: a stop that cannot fail to fill
    assert sl["slTriggerPxType"] == "mark"  # mark, not last — wicks don't trigger it


def test_isolated_net_mode_and_client_order_id_are_always_sent() -> None:
    params = build_bracket_params(_order())
    assert params["tdMode"] == "isolated"
    assert params["posSide"] == "net"
    assert params["clOrdId"] == "a1b2c3d4e5f6a7b8"


def test_partial_take_profit_carries_its_own_size() -> None:
    params = build_bracket_params(_order())
    tp = next(a for a in params["attachAlgoOrds"] if "tpTriggerPx" in a)
    tp.pop("attachAlgoClOrdId")  # covered by test_algo_client_ids.py
    assert tp == {"tpTriggerPx": "80000", "tpOrdPx": "-1", "sz": "0.5"}


def test_trailing_callback_is_sent_as_a_ratio_not_a_percentage() -> None:
    order = _order(
        attach_algos=[
            {"kind": "sl", "trigger_px": Decimal("72000")},
            {"kind": "trailing", "callback_pct": Decimal("2.5"), "trigger_px": Decimal("77250")},
        ]
    )
    attached = build_bracket_params(order)["attachAlgoOrds"]
    trailing = next(a for a in attached if "callbackRatio" in a)
    assert trailing["callbackRatio"] == "0.025"  # OKX wants 0.025, not 2.5
    assert trailing["activePx"] == "77250"


def test_a_bracket_without_a_stop_is_refused() -> None:
    order = _order(attach_algos=[{"kind": "tp", "trigger_px": Decimal("80000")}])
    with pytest.raises(ValueError, match="attach a stop-loss"):
        build_bracket_params(order)


def test_unknown_algo_kind_is_refused() -> None:
    order = _order(attach_algos=[{"kind": "moon", "trigger_px": Decimal("1")}])
    with pytest.raises(ValueError, match="unknown attached algo kind"):
        build_bracket_params(order)


def test_post_only_is_passed_through_for_limit_only_regimes() -> None:
    assert build_bracket_params(_order(post_only=True))["ordType"] == "post_only"
    assert "ordType" not in build_bracket_params(_order())
