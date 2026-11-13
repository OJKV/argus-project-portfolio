def test_a_long_never_becomes_a_sell() -> None:
    """Every long was being sent to the venue as a SELL (F-137).

    `build_entry_order` converts the plan's `long` into `buy`, and `place_bracket` then asked
    `order["side"] == "long"` — False for `"buy"` — so it fell to the else branch. OKX caught it
    only indirectly, rejecting the attached stop with 51049 because a long's stop sits on the
    wrong side of a short. Nothing in the unit tests noticed, because the fake adapter reads the
    side straight from the order and never re-derives it.
    """
    from argus.exchange.okx import venue_side

    assert venue_side("long") == "buy"
    assert venue_side("buy") == "buy"
    assert venue_side("short") == "sell"
    assert venue_side("sell") == "sell"


def test_an_unrecognised_side_is_refused_rather_than_defaulted() -> None:
    """Two vocabularies meeting at one boundary is the hazard. A silent default is how a long
    becomes a short, and the venue is the only place that would ever notice."""
    import pytest

    from argus.exchange.okx import venue_side

    for bad in ("up", "down", "", "LONGG", "0"):
        with pytest.raises(ValueError, match="unrecognised side"):
            venue_side(bad)


def test_the_whole_entry_path_keeps_the_direction() -> None:
    """From the gate's PlanInput through build_entry_order to the venue's side, both ways."""
    from decimal import Decimal

    from argus.exchange.okx import venue_side
    from argus.execution.orders import build_entry_order
    from argus.gate.rules import PlanInput, Target
    from argus.gate.sizing import SizingResult
    from argus.llm.schemas import FactorScores

    def _sized() -> SizingResult:
        return SizingResult(
            ok=True,
            reason="",
            step=0,
            risk_usd=10.0,
            stop_dist_pct=4.0,
            notional_usd=300.0,
            leverage=2,
            margin_usd=150.0,
            contracts=Decimal("3"),
            liq_dist_pct=20.0,
        )

    scores = FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2)
    long_plan = PlanInput(
        symbol="BTC",
        side="long",
        entry_kind="limit",
        entry_low=99.8,
        entry_high=100.2,
        stop=96.0,
        targets=[Target(price=110.0, close_pct=100)],
        time_stop_hours=96,
        scores=scores,
    )
    short_plan = PlanInput(
        symbol="BTC",
        side="short",
        entry_kind="limit",
        entry_low=99.8,
        entry_high=100.2,
        stop=104.0,
        targets=[Target(price=90.0, close_pct=100)],
        time_stop_hours=96,
        scores=scores,
    )

    long_order = build_entry_order(long_plan, _sized(), "r1", "BTC-USDT-SWAP")
    short_order = build_entry_order(short_plan, _sized(), "r2", "BTC-USDT-SWAP")

    assert venue_side(long_order["side"]) == "buy"
    assert venue_side(short_order["side"]) == "sell"
    # And the stop stays on the protective side of each.
    assert long_order["attach_algos"][0]["trigger_px"] < Decimal("99.8")
    assert short_order["attach_algos"][0]["trigger_px"] > Decimal("100.2")
