"""Fee viability — gate 13 (DECISION_LOGIC.md §6; FAILURE_LOG F-026, F-036, F-042).

Real rates came in 6–12x the documented ones in the case study, so rates always arrive from the
account endpoint. OKX reports charges as negative numbers; the gate works in absolute cost.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from argus.gate.fees import FeeRates, expected_costs, fee_viable


def _rates(taker: str = "-0.0005", maker: str = "-0.0002") -> FeeRates:
    return FeeRates(maker=Decimal(maker), taker=Decimal(taker))


def test_round_trip_cost_counts_both_sides() -> None:
    costs = expected_costs(
        notional=1000.0, rates=_rates(), entry_is_maker=False, funding_8h=0.0, hours=24
    )
    assert costs["fees"] == pytest.approx(1.0)  # 0.05% in + 0.05% out
    assert costs["funding"] == 0.0
    assert costs["total"] == pytest.approx(1.0)


def test_a_maker_entry_pays_the_maker_rate_on_the_way_in() -> None:
    costs = expected_costs(
        notional=1000.0, rates=_rates(), entry_is_maker=True, funding_8h=0.0, hours=24
    )
    assert costs["fees"] == pytest.approx(0.2 + 0.5)


def test_funding_accrues_per_eight_hour_period_and_can_be_a_credit() -> None:
    longs = expected_costs(
        notional=1000.0,
        rates=_rates(),
        entry_is_maker=False,
        funding_8h=0.0001,
        hours=24,
        side="long",
    )
    shorts = expected_costs(
        notional=1000.0,
        rates=_rates(),
        entry_is_maker=False,
        funding_8h=0.0001,
        hours=24,
        side="short",
    )
    assert longs["funding"] == pytest.approx(0.3)  # 3 periods x 0.01% x 1000
    assert shorts["funding"] == pytest.approx(-0.3)  # shorts are paid when funding is positive


def test_projected_funding_income_never_offsets_certain_fees() -> None:
    """G-8: a projected funding credit was netted against fees that are certain. Funding flips;
    a 22 %-of-TP1 fee load passed as -0.05 % because the short expected to be paid."""
    shorts = expected_costs(
        notional=1000.0,
        rates=_rates(),
        entry_is_maker=False,
        funding_8h=0.0010,
        hours=72,
        side="short",
    )
    assert shorts["funding"] < 0  # still reported as the credit it is
    assert shorts["total"] == pytest.approx(shorts["fees"])
    ok, _ = fee_viable(
        notional=1000.0,
        expected_tp1_profit=4.5,
        rates=_rates(),
        entry_is_maker=False,
        funding_8h=0.0010,
        hours=72,
        side="short",
    )
    assert not ok  # $1.00 of fees is 22 % of $4.50, credit or no credit


def test_a_trade_that_cannot_pay_for_itself_is_refused() -> None:
    ok, detail = fee_viable(
        notional=1000.0,
        expected_tp1_profit=4.0,
        rates=_rates(),
        entry_is_maker=False,
        funding_8h=0.0,
        hours=24,
    )
    assert not ok  # $1.00 of fees is 25% of a $4 target, over the 20% limit
    assert "25.0%" in detail


def test_a_trade_with_room_to_pay_its_costs_passes() -> None:
    ok, _ = fee_viable(
        notional=1000.0,
        expected_tp1_profit=20.0,
        rates=_rates(),
        entry_is_maker=False,
        funding_8h=0.0,
        hours=24,
    )
    assert ok


def test_a_positive_taker_rate_is_refused_rather_than_trusted() -> None:
    # OKX charges are negative; a positive rate means the convention changed (F-036).
    with pytest.raises(ValueError, match="positive"):
        expected_costs(
            notional=1000.0,
            rates=FeeRates(maker=Decimal("-0.0002"), taker=Decimal("0.0005")),
            entry_is_maker=False,
            funding_8h=0.0,
            hours=24,
        )


def test_zero_expected_profit_is_never_viable_and_never_divides_by_zero() -> None:
    ok, detail = fee_viable(
        notional=1000.0,
        expected_tp1_profit=0.0,
        rates=_rates(),
        entry_is_maker=False,
        funding_8h=0.0,
        hours=24,
    )
    assert not ok and "no expected profit" in detail
