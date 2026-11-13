"""The Risk Gate: 14 gates in order (DECISION_LOGIC.md §6). The LLM cannot bypass any of them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from argus.features.regime import RegimeInputs, RegimeView, classify
from argus.gate.fees import FeeRates
from argus.gate.killswitch import KillRule, Triggered
from argus.gate.protections import TradingHistory
from argus.gate.rules import (
    FactorScores,
    MarketContext,
    OpenPosition,
    PlanInput,
    PortfolioState,
    Target,
    evaluate,
)
from argus.gate.sizing import InstrumentSpec, StepPolicy

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
SPEC = InstrumentSpec(
    contract_value=Decimal("1"),
    lot_size=Decimal("0.1"),
    min_contracts=Decimal("0.1"),
    max_leverage=50,
    maint_margin_rate=Decimal("0.01"),
)
STEPS = (
    StepPolicy(0, 1.0, 10, 2, 0.25),
    StepPolicy(1, 2.0, 10, 2, 0.40),
    StepPolicy(2, 3.5, 10, 2, 0.50),
    StepPolicy(3, 5.0, 10, 5, 0.60),
)


def _regime(label: str = "TREND_UP") -> RegimeView:
    if label == "TREND_UP":
        inputs = RegimeInputs(
            btc_close=100,
            btc_ema20=95,
            btc_ema50=90,
            btc_ema20_slope=0.5,
            btc_rv20_pct=40,
            breadth_20=60,
            next_event_hours=72,
        )
    elif label == "TREND_DOWN":
        inputs = RegimeInputs(
            btc_close=90,
            btc_ema20=95,
            btc_ema50=100,
            btc_ema20_slope=-0.5,
            btc_rv20_pct=40,
            breadth_20=30,
            next_event_hours=72,
        )
    else:
        inputs = RegimeInputs(
            btc_close=100,
            btc_ema20=95,
            btc_ema50=90,
            btc_ema20_slope=0.5,
            btc_rv20_pct=40,
            breadth_20=50,
            next_event_hours=72,
        )
    return classify(inputs)


def _scores(**over: int) -> FactorScores:
    base = {"fundamental": 1, "onchain": 1, "macro": 0, "catalyst": 1, "technical": 2}
    base.update(over)
    return FactorScores(**base)


def _plan(**over: Any) -> PlanInput:
    base: dict[str, Any] = {
        "symbol": "SOL",
        "side": "long",
        "entry_kind": "limit",
        "entry_low": 99.0,
        "entry_high": 101.0,
        "stop": 94.0,
        "targets": [Target(110.0, 50), Target(120.0, 50)],
        "time_stop_hours": 120,
        "scores": _scores(),
        "event_thesis": False,
        "urgent": False,
        "invalidation_rules": [{"kind": "price_close_below", "level": 94}],
    }
    base.update(over)
    return PlanInput(**base)


def _market(**over: Any) -> MarketContext:
    base: dict[str, Any] = {
        "mark": 100.0,
        "atr_pct": 2.5,
        "spread_pct": 0.01,
        "funding_8h": 0.0001,
        "swing_high_20": 108.0,
        "swing_low_20": 95.0,
        "regime": _regime(),
        "spec": SPEC,
        "fees": FeeRates(maker=Decimal("-0.0002"), taker=Decimal("-0.0005")),
        "consensus_ok": True,
        "derivatives_age_min": 5.0,
        "news_present": True,
        "suspect_sources": [],
        "next_event_hours": 72.0,
        "in_market_list": True,
        "beta_btc": 1.1,
    }
    base.update(over)
    return MarketContext(**base)


def _history(**over: Any) -> TradingHistory:
    base: dict[str, Any] = {
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "consecutive_losses": 0,
        "entries_this_run": 0,
        "entries_today": 0,
        "stop_outs_by_symbol": {},
        "last_loss_at": None,
        "trades_since_pause": 99,
    }
    base.update(over)
    return TradingHistory(**base)


def _portfolio(**over: Any) -> PortfolioState:
    base: dict[str, Any] = {
        "equity": 2000.0,
        "free_margin": 1800.0,
        "positions": [],
        "history": _history(),
        "peak_equity_30d": 2000.0,
        "margin_ratio": None,
        "downside_miss_rate": None,
        "active_kill_switches": [],
    }
    base.update(over)
    # A book with positions reports a healthy margin unless a test says otherwise: an unknown
    # ratio with open positions refuses entries (Muse P0-2).
    if base["positions"] and "margin_ratio" not in over:
        base["margin_ratio"] = 900.0
    # An account at its own high unless a test says otherwise: gate 11 reads the drawdown, and
    # a $150 account under a $2000 peak is a 92 % drawdown, not a small account.
    if "peak_equity_30d" not in over:
        base["peak_equity_30d"] = base["equity"]
    return PortfolioState(**base)


def _run(
    plan: PlanInput | None = None,
    market: MarketContext | None = None,
    portfolio: PortfolioState | None = None,
) -> Any:
    return evaluate(plan or _plan(), market or _market(), portfolio or _portfolio(), STEPS, NOW)


def _gates(result: Any) -> dict[int, str]:
    return {r.gate: r.verdict for r in result.reasons}


def test_a_clean_plan_is_approved_and_sized() -> None:
    result = _run()
    assert result.status == "APPROVED"
    assert result.sized is not None and result.sized.ok
    assert result.sized.notional_usd > 0
    assert 2 <= result.sized.leverage <= 10


# --- the plan itself (G-6) ------------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        # NaN: `rr1 < MIN` is False for NaN and sorted([110, nan]) counts as ordered
        {"targets": [Target(110.0, 50), Target(float("nan"), 50)]},
        {"stop": float("nan")},
        {"entry_low": -99.0},
        {"entry_low": 102.0, "entry_high": 101.0},  # an inverted band
        # close_pct 150/-50 sums to 100 and triples TP1's share of profit for gates 7 and 13
        {"targets": [Target(110.0, 150), Target(120.0, -50)]},
        {"time_stop_hours": 0},
        {"time_stop_hours": 10_000},
        {"targets": []},
        {"size_factor": float("nan")},
    ],
)
def test_a_malformed_plan_is_rejected_before_any_gate_reads_it(over: dict[str, Any]) -> None:
    """G-6: `PlanInput` is a bare dataclass, and the stub proposer never passed the LLM schema."""
    result = _run(_plan(**over))
    assert result.status == "REJECTED"
    assert result.reasons[0].gate == 0 and result.reasons[0].verdict == "REJECT"


def test_the_gate_and_the_llm_schema_bound_the_time_stop_alike() -> None:
    from argus.gate.rules import MAX_TIME_STOP_HOURS
    from argus.llm.schemas import MAX_TIME_STOP_HOURS as SCHEMA_MAX

    assert MAX_TIME_STOP_HOURS == SCHEMA_MAX


def test_scores_outside_the_scale_are_rejected_not_counted() -> None:
    """G-6: the veto was `-2 in scores`, so -3 was not a veto, and +5 inflated the total."""
    below = _run(_plan(scores=_scores(fundamental=-3, onchain=2, macro=2, catalyst=2)))
    assert below.status == "REJECTED"
    inflated = _run(_plan(scores=_scores(technical=5, onchain=0, catalyst=0, fundamental=0)))
    assert inflated.status == "REJECTED"


# --- gate 1: score --------------------------------------------------------------------------


def test_gate_1_rejects_a_total_below_four() -> None:
    result = _run(_plan(scores=_scores(fundamental=0, onchain=0, catalyst=0, technical=1)))
    assert result.status == "REJECTED" and _gates(result)[1] == "REJECT"


def test_gate_1_rejects_any_minus_two_however_high_the_total() -> None:
    result = _run(
        _plan(scores=_scores(fundamental=2, onchain=2, macro=-2, catalyst=2, technical=2))
    )
    assert result.status == "REJECTED"
    assert "veto" in next(r.detail for r in result.reasons if r.gate == 1)


def test_gate_1_demands_six_for_a_counter_regime_trade() -> None:
    market = _market(regime=_regime("TREND_DOWN"))
    assert _run(_plan(scores=_scores()), market).status == "REJECTED"  # +5 long in a downtrend
    strong = _plan(scores=_scores(fundamental=2, onchain=2, macro=0, catalyst=1, technical=2))
    assert _run(strong, market).status in {"APPROVED", "ADJUSTED"}


# --- gate 2: data quality -------------------------------------------------------------------


def test_gate_2_rejects_when_price_consensus_failed() -> None:
    result = _run(market=_market(consensus_ok=False))
    assert result.status == "REJECTED" and _gates(result)[2] == "REJECT"


def test_gate_2_rejects_stale_derivatives() -> None:
    assert _run(market=_market(derivatives_age_min=180.0)).status == "REJECTED"


def test_gate_2_rejects_an_unmeasured_beta_instead_of_calling_it_low() -> None:
    """D-10: `(beta_btc or 0) > HIGH_BETA` read an unknown beta as 0, so a high-beta alt with no
    beta history skipped the same-side correlation cap in gate 6 entirely."""
    crowded = _portfolio(
        positions=[
            OpenPosition("ARB", "long", 200.0, 10.0, 1.8),
            OpenPosition("OP", "long", 200.0, 10.0, 1.9),
        ]
    )
    result = _run(market=_market(beta_btc=None), portfolio=crowded)
    assert result.status == "REJECTED"
    assert _gates(result)[2] == "REJECT"
    assert _run(market=_market(beta_btc=float("nan"))).status == "REJECTED"


def test_gate_2_halves_size_when_only_news_is_missing() -> None:
    result = _run(market=_market(news_present=False))
    assert result.status == "ADJUSTED"
    assert result.size_mult == pytest.approx(0.5)


def test_gate_2_rejects_when_cited_evidence_is_suspect() -> None:
    assert _run(market=_market(suspect_sources=["loris.tools"])).status == "REJECTED"


# --- gate 3: structure ----------------------------------------------------------------------


def test_gate_3_rejects_a_stop_outside_the_atr_band() -> None:
    assert _run(_plan(stop=99.0)).status == "REJECTED"  # 1% stop vs 2.5% ATR is under 1x
    assert _run(_plan(stop=80.0)).status == "REJECTED"  # 20% stop is over 3.5x


def test_gate_3_rejects_a_stop_inside_structure() -> None:
    result = _run(_plan(stop=96.0), _market(swing_low_20=95.0))
    assert result.status == "REJECTED"
    assert "structure" in next(r.detail for r in result.reasons if r.gate == 3)


def test_gate_3_rejects_when_the_structure_level_is_unknown() -> None:
    """G-14: a missing 20-bar swing skipped the "stop beyond structure" check — fail open."""
    assert _gates(_run(market=_market(swing_low_20=None)))[3] == "REJECT"
    short = _plan(
        side="short",
        stop=106.0,
        targets=[Target(90.0, 50), Target(80.0, 50)],
        scores=_scores(catalyst=2),  # +6: a short against TREND_UP is counter-regime
    )
    assert _gates(_run(short, _market(swing_high_20=None)))[3] == "REJECT"


def test_gate_3_rejects_a_thin_reward_to_risk() -> None:
    assert _run(_plan(targets=[Target(102.0, 100)])).status == "REJECTED"


def test_gate_3_rejects_unordered_or_incomplete_targets() -> None:
    assert _run(_plan(targets=[Target(120.0, 50), Target(110.0, 50)])).status == "REJECTED"
    assert _run(_plan(targets=[Target(110.0, 40), Target(120.0, 40)])).status == "REJECTED"


def test_gate_3_rejects_an_entry_zone_far_from_price() -> None:
    assert _run(_plan(entry_low=108.0, entry_high=110.0)).status == "REJECTED"


def test_gate_3_rejects_a_long_limit_zone_wholly_above_the_mark() -> None:
    """2026-10-29: a BTC long limit at 80,500 with the mark below the whole zone. A limit above
    the market is a market buy before the level the plan waited for, and OKX refused it (51006),
    failing the run. The gate refuses it instead."""
    result = _run(market=_market(mark=98.5))
    assert result.status == "REJECTED"
    assert "above the mark" in next(r.detail for r in result.reasons if r.gate == 3)


def test_gate_3_rejects_a_short_limit_zone_wholly_below_the_mark() -> None:
    short = _plan(side="short", stop=108.5, targets=[Target(90.0, 50), Target(80.0, 50)])
    result = _run(short, _market(mark=101.5, regime=_regime("TREND_DOWN")))
    assert result.status == "REJECTED"
    assert "below the mark" in next(r.detail for r in result.reasons if r.gate == 3)


def test_gate_3_keeps_a_long_limit_resting_below_the_mark() -> None:
    assert _run(market=_market(mark=101.5)).status == "APPROVED"


def test_gate_3_keeps_a_long_zone_whose_low_edge_is_at_the_mark() -> None:
    # A buy limit at the mark sits below the ask and rests; only a zone wholly above is refused.
    assert _run(market=_market(mark=99.0)).status == "APPROVED"


def _urgent(**over: Any) -> PlanInput:
    return _plan(entry_kind="market", urgent=True, **over)


def test_gate_3_rejects_a_market_entry_when_price_has_left_the_entry_band() -> None:
    """G-N3 / G-2: plan band 99-101, stop 94, mark 104. A market order fills at 104, where the
    real stop distance is 9.6 % — not the 6 % the plan was scored and sized on."""
    result = _run(_urgent(), _market(mark=104.0))
    assert result.status == "REJECTED"
    assert _gates(result)[3] == "REJECT"


def test_a_market_entry_is_sized_on_the_mark_it_will_fill_at() -> None:
    # RR is measured from the fill too, so give the targets room for a fill at 100.8.
    result = _run(_urgent(targets=[Target(112.0, 50), Target(124.0, 50)]), _market(mark=100.8))
    assert result.status != "REJECTED", result.reasons[-1]
    assert result.sized is not None
    # (100.8 - 94) / 100.8, not the plan mid's (100 - 94) / 100.
    assert result.sized.stop_dist_pct == pytest.approx(6.8 / 100.8)


# --- gates 4/5: size and leverage -----------------------------------------------------------


def test_gates_4_and_5_produce_risk_based_size_within_the_leverage_cap() -> None:
    # F-002: size must follow stop distance, not a fixed fraction of balance.
    wide = _run(_plan(stop=93.0, targets=[Target(115.0, 50), Target(125.0, 50)]))
    tight = _run(_plan(stop=94.9, targets=[Target(110.0, 50), Target(118.0, 50)]))
    assert wide.sized is not None and tight.sized is not None
    assert tight.sized.notional_usd > wide.sized.notional_usd  # tighter stop → bigger size
    assert wide.sized.leverage <= 10 and tight.sized.leverage <= 10


def test_gate_5_keeps_liquidation_beyond_the_stop() -> None:
    result = _run()
    assert result.sized is not None
    assert result.sized.liq_dist_pct >= 2.5 * result.sized.stop_dist_pct


# --- gate 6: portfolio heat -----------------------------------------------------------------


def test_gate_6_rejects_a_fifth_position() -> None:
    positions = [OpenPosition(f"A{i}", "long", 500.0, 20.0, 1.0) for i in range(4)]
    assert _run(portfolio=_portfolio(positions=positions)).status == "REJECTED"


def test_gate_6_rejects_when_open_risk_already_fills_the_budget() -> None:
    positions = [OpenPosition("ETH", "long", 500.0, 95.0, 1.0)]  # 4.75% of 2000 equity
    result = _run(portfolio=_portfolio(positions=positions))
    assert result.status == "REJECTED"
    assert "open risk" in next(r.detail for r in result.reasons if r.gate == 6)


def test_gate_6_rejects_a_third_same_direction_high_beta_alt() -> None:
    positions = [
        OpenPosition("ARB", "long", 300.0, 10.0, 1.6),
        OpenPosition("SUI", "long", 300.0, 10.0, 1.5),
    ]
    result = _run(market=_market(beta_btc=1.4), portfolio=_portfolio(positions=positions))
    assert result.status == "REJECTED"


def test_gate_6_allows_the_same_names_on_the_other_side() -> None:
    positions = [
        OpenPosition("ARB", "short", 300.0, 10.0, 1.6),
        OpenPosition("SUI", "short", 300.0, 10.0, 1.5),
    ]
    assert (
        _run(market=_market(beta_btc=1.4), portfolio=_portfolio(positions=positions)).status
        != "REJECTED"
    )


# --- gate 7: funding ------------------------------------------------------------------------


def test_gate_7_rejects_when_funding_eats_the_target() -> None:
    result = _run(_plan(time_stop_hours=168), _market(funding_8h=0.01))
    assert result.status == "REJECTED"
    assert _gates(result)[7] == "REJECT"


# --- gate 8: events -------------------------------------------------------------------------


def test_gate_8_blocks_an_entry_into_a_high_impact_event() -> None:
    # F-014: ETH/BTC longs survived a CPI flush by luck.
    assert _run(market=_market(next_event_hours=4.0)).status == "REJECTED"


def test_gate_8_allows_a_deliberate_event_thesis() -> None:
    """A corroborated event thesis: the model says so, scored the catalyst, and the dossier has
    a dated catalyst inside the blackout. It still trades at the event-risk size."""
    result = _run(
        _plan(event_thesis=True), _market(next_event_hours=4.0, catalyst_dates=(NOW.date(),))
    )
    assert result.status == "ADJUSTED"
    assert result.size_mult == pytest.approx(0.5)
    assert _gates(result)[8] == "ADJUST"


def test_gate_8_ignores_an_event_thesis_with_nothing_behind_it() -> None:
    """G-N6 / L-2: `event_thesis` is a bare LLM boolean, never mentioned in the Trader prompt, so
    it is set on a whim or by an injected headline. FOMC in 4h, catalyst score 0, no dated
    catalyst: the blackout holds."""
    bare = _run(
        _plan(event_thesis=True, scores=_scores(catalyst=0, technical=2, onchain=2)),
        _market(next_event_hours=4.0, catalyst_dates=(NOW.date(),)),
    )
    assert bare.status == "REJECTED" and _gates(bare)[8] == "REJECT"
    undated = _run(_plan(event_thesis=True), _market(next_event_hours=4.0))
    assert undated.status == "REJECTED" and _gates(undated)[8] == "REJECT"
    elsewhere = _run(
        _plan(event_thesis=True),
        _market(next_event_hours=4.0, catalyst_dates=((NOW + timedelta(days=3)).date(),)),
    )
    assert elsewhere.status == "REJECTED" and _gates(elsewhere)[8] == "REJECT"


def test_gate_8_forces_limit_only_in_an_event_risk_regime() -> None:
    inputs = RegimeInputs(
        btc_close=100,
        btc_ema20=95,
        btc_ema50=90,
        btc_ema20_slope=0.5,
        btc_rv20_pct=95,
        breadth_20=60,
        next_event_hours=72,
    )
    market = _market(regime=classify(inputs))
    assert _run(_plan(entry_kind="market", urgent=True), market).status == "REJECTED"


# --- gate 9: protections --------------------------------------------------------------------


def test_gate_9_applies_the_portfolio_protections() -> None:
    result = _run(portfolio=_portfolio(history=_history(daily_pnl_pct=-3.5)))
    assert result.status == "REJECTED" and _gates(result)[9] == "REJECT"


# --- gate 10: sanity ------------------------------------------------------------------------


def test_gate_10_rejects_an_unlisted_symbol() -> None:
    assert _run(market=_market(in_market_list=False)).status == "REJECTED"


def test_gate_10_rejects_a_duplicate_or_opposite_position() -> None:
    same = [OpenPosition("SOL", "long", 300.0, 10.0, 1.0)]
    opposite = [OpenPosition("SOL", "short", 300.0, 10.0, 1.0)]
    assert _run(portfolio=_portfolio(positions=same)).status == "REJECTED"
    assert _run(portfolio=_portfolio(positions=opposite)).status == "REJECTED"


def test_gate_10_rejects_an_inconsistent_side() -> None:
    assert _run(_plan(side="short")).status == "REJECTED"  # stop below entry on a short


def test_gate_10_rejects_a_market_entry_that_is_not_urgent() -> None:
    assert _run(_plan(entry_kind="market")).status == "REJECTED"
    assert (
        _run(_plan(entry_kind="market", urgent=True), _market(spread_pct=0.05)).status == "REJECTED"
    )
    assert (
        _run(_plan(entry_kind="market", urgent=True), _market(spread_pct=0.02)).status != "REJECTED"
    )


# --- gate 11: kill-switch conflict ----------------------------------------------------------


def test_gate_11_rejects_a_plan_fighting_an_active_kill_switch() -> None:
    fired = [Triggered(KillRule("price_close_below", level=95.0), "1d close 94 < 95")]
    result = _run(portfolio=_portfolio(active_kill_switches=fired))
    assert result.status == "REJECTED" and _gates(result)[11] == "REJECT"


def test_gate_11_rejects_new_entries_in_a_portfolio_drawdown() -> None:
    """§9: equity 15 % below its 30-day high closes everything and HALTs. `peak_equity_30d` was
    handed to the gate and never read, so the manager could add risk into that drawdown."""
    result = _run(portfolio=_portfolio(equity=1700.0, free_margin=1500.0, peak_equity_30d=2000.0))
    assert result.status == "REJECTED" and _gates(result)[11] == "REJECT"
    assert result.halt
    assert "drawdown" in result.reasons[-1].detail


def test_gate_11_rejects_new_entries_while_margin_must_be_reduced() -> None:
    result = _run(portfolio=_portfolio(margin_ratio=150.0))
    assert result.status == "REJECTED" and _gates(result)[11] == "REJECT"
    assert not result.halt
    assert "margin ratio" in result.reasons[-1].detail


def test_gate_11_passes_a_drawdown_inside_the_limit_and_a_healthy_margin() -> None:
    result = _run(portfolio=_portfolio(equity=1800.0, peak_equity_30d=2000.0, margin_ratio=900.0))
    assert result.status == "APPROVED", result.reasons[-1]


def test_gate_11_rejects_an_invalidation_rule_it_cannot_evaluate() -> None:
    """G-4 / F-013: `invalidation_rules` were journalled raw and never parsed, so `vibes` was
    APPROVED. A plan whose invalidation cannot be evaluated has no invalidation."""
    for rules in ([{"kind": "vibes"}], [{"kind": "price_close_below", "level": float("nan")}]):
        result = _run(_plan(invalidation_rules=rules))
        assert result.status == "REJECTED" and _gates(result)[11] == "REJECT"


def test_parsed_invalidation_rules_are_journalled_in_their_typed_form() -> None:
    result = _run(_plan(invalidation_rules=[{"kind": "PRICE_CLOSE_BELOW", "level": "94"}]))
    assert result.status != "REJECTED"
    assert result.as_dict()["rules"] == [
        {
            "kind": "price_close_below",
            "level": 94.0,
            "sign": None,
            "event": None,
            "outcome": None,
            "timeframe": "1d",
        }
    ]


# --- gate 12: balance -----------------------------------------------------------------------


def test_gate_12_rejects_when_free_margin_cannot_cover_it() -> None:
    result = _run(portfolio=_portfolio(free_margin=5.0))
    assert result.status == "REJECTED"
    assert "INSUFFICIENT_BALANCE" in next(r.detail for r in result.reasons if r.gate == 12)


def test_gate_12_escalates_small_account_steps_before_giving_up() -> None:
    # F-031: at $150 equity, step 0 is below the viable floor; steps 1+ must be tried.
    # A 7% stop at $150 equity puts step 0 (1% risk → $21 notional) under the $25 viable floor.
    result = _run(
        _plan(stop=93.0, targets=[Target(115.0, 50), Target(125.0, 50)]),
        portfolio=_portfolio(equity=150.0, free_margin=140.0),
    )
    assert result.sized is not None and result.sized.step >= 1


# --- gate 13: fees --------------------------------------------------------------------------


def test_gate_13_rejects_a_trade_that_cannot_pay_its_costs() -> None:
    result = _run(_plan(targets=[Target(100.6, 100)], stop=97.5))
    assert result.status == "REJECTED"
    assert 13 in _gates(result) or 3 in _gates(result)  # thin target fails RR or fees


def test_gate_13_costs_a_limit_entry_at_taker_unless_it_is_post_only() -> None:
    """G-10: every limit entry was costed at the maker rate. Our limits are not post-only, and a
    marketable limit in a trend fills taker — §6 gate 13 says assume taker. With a 0.5 % taker
    rate this trade's costs are 13 % of TP1 at maker and 23 % at taker: past the 20 % limit."""
    expensive = FeeRates(maker=Decimal("-0.0002"), taker=Decimal("-0.005"))
    result = _run(market=_market(fees=expensive))
    assert result.status == "REJECTED" and _gates(result)[13] == "REJECT"


def test_a_risk_debate_reduction_on_a_small_account_is_not_escalated_away() -> None:
    """G-N2: $150, stop 6 %: step 0 is exactly the $25 floor. `reduce` (x0.5) must not climb to
    step 1 and place the same $25 order — it is half that size, or nothing."""
    small = _portfolio(equity=150.0, free_margin=150.0)
    full = _run(portfolio=small)
    assert full.sized is not None and full.sized.step == 0
    halved = _run(plan=_plan(size_factor=0.5), portfolio=small)
    if halved.status == "REJECTED":
        assert _gates(halved)[4] == "REJECT"
    else:
        assert halved.sized is not None
        assert halved.sized.risk_usd <= full.sized.risk_usd * 0.5 + 1e-9


def test_a_step_3_trade_is_judged_on_its_placed_risk_not_on_rounding() -> None:
    """G-20: a step-3 budget is exactly the 5 % heat cap, so 4-dp rounding of the nominal risk
    decided the verdict ($150.123456 rejected, $150.00 passed). Judged on the risk of the
    contracts actually placed, the two accounts get the same answer."""
    step3 = (StepPolicy(3, 5.0, 10, 5, 0.60),)
    verdicts = set()
    for equity in (150.0, 150.123456):
        result = evaluate(
            _plan(), _market(), _portfolio(equity=equity, free_margin=equity), step3, NOW
        )
        assert result.status != "REJECTED", result.reasons[-1]
        assert result.sized is not None and result.sized.risk_usd / equity * 100 <= 5.0
        verdicts.add(result.status)
    assert len(verdicts) == 1


def test_gate_7_counts_funding_on_the_instruments_own_interval() -> None:
    """G-9: funding was assumed to settle every 8 h. OKX settles many alts every 4/2/1 h, so the
    same per-period rate costs 2-8x more over the hold than the gate counted."""
    eight = _run(market=_market(funding_8h=0.0003, funding_interval_hours=8.0))
    hourly = _run(market=_market(funding_8h=0.0003, funding_interval_hours=1.0))
    assert eight.status != "REJECTED", eight.reasons[-1]
    assert hourly.status == "REJECTED" and _gates(hourly)[7] == "REJECT"


def test_gate_2_rejects_an_unknown_funding_interval() -> None:
    result = _run(market=_market(funding_interval_hours=None))
    assert result.status == "REJECTED" and _gates(result)[2] == "REJECT"


# --- gate 14: downside-miss monitor ---------------------------------------------------------


def test_gate_14_caps_leverage_when_stops_have_been_slipping() -> None:
    result = _run(portfolio=_portfolio(downside_miss_rate=0.30))
    assert result.sized is not None and result.sized.leverage <= 5
    assert result.leverage_cap == 5
    assert _gates(result)[14] == "ADJUST"


def test_gate_14_widens_the_liquidation_buffer_enough_to_refuse_a_trade() -> None:
    """G-18: the test above cannot fail — leverage is always the step minimum (G-12, open). This
    one can: at step 3 (5x) a 7 % stop clears a 2.5x liquidation buffer (18.8 % >= 17.5 %) but
    not the 3x one gate 14 imposes (21 %), so slipping stops must turn approval into refusal."""
    step3 = (StepPolicy(3, 5.0, 10, 5, 0.60),)
    plan = _plan(stop=93.0, targets=[Target(112.0, 50), Target(125.0, 50)])
    calm = evaluate(plan, _market(), _portfolio(), step3, NOW)
    slipping = evaluate(plan, _market(), _portfolio(downside_miss_rate=0.30), step3, NOW)
    assert calm.status != "REJECTED", calm.reasons[-1]
    assert slipping.status == "REJECTED"


def test_a_zero_mark_rejects_at_gate_two_instead_of_dividing_by_it() -> None:
    """G-18 / G-19: `mark=0` was a ZeroDivisionError outside any handler."""
    result = _run(market=_market(mark=0.0))
    assert result.status == "REJECTED" and _gates(result)[2] == "REJECT"


# --- cumulative adjustment ------------------------------------------------------------------


def test_a_plan_shrunk_below_half_its_implied_size_is_rejected() -> None:
    # "A heavily-adjusted plan is a different trade than the one scored."
    result = _run(
        market=_market(news_present=False),
        portfolio=_portfolio(
            history=_history(
                consecutive_losses=3, last_loss_at=NOW - timedelta(hours=30), trades_since_pause=0
            )
        ),
    )
    assert result.status == "REJECTED"
    assert any("below 50%" in r.detail for r in result.reasons)


def test_every_gate_decision_is_recorded_for_the_journal() -> None:
    result = _run()
    assert [r.gate for r in result.reasons] == sorted(r.gate for r in result.reasons)
    assert all(r.name and r.verdict in {"PASS", "ADJUST", "REJECT"} for r in result.reasons)
    assert result.as_dict()["status"] == "APPROVED"


def test_gates_run_in_order_and_stop_at_the_first_rejection() -> None:
    result = _run(_plan(scores=_scores(fundamental=-2)), _market(consensus_ok=False))
    assert result.status == "REJECTED"
    assert max(r.gate for r in result.reasons) == 1  # gate 1 rejected; gate 2 never ran


def test_no_gate_rule_reads_the_thesis() -> None:
    """`PlanInput.thesis` is carried for the journal and must never reach a decision.

    `to_plan_input` is lossy on purpose: nothing the model writes in free text can influence
    sizing. The Reflector needs the thesis though, so it crosses the boundary as a field the gate
    is forbidden to consult — and "forbidden" has to be checked, not trusted (F-130).
    """
    import inspect

    from argus.gate import protections, rules, sizing

    for module in (rules, sizing, protections):
        source = inspect.getsource(module)
        body = source.split("thesis: str = ", 1)[-1] if module is rules else source
        offenders = [
            line.strip()
            for line in body.splitlines()
            if ".thesis" in line and "event_thesis" not in line and not line.strip().startswith("#")
        ]
        assert not offenders, f"{module.__name__} reads the thesis: {offenders}"


@pytest.mark.parametrize("funding", [-0.0001, -0.00001])
def test_negative_funding_is_a_measurement_not_missing_data(funding: float) -> None:
    """Funding is signed: shorts pay longs whenever it is negative, a common state for alts. Gate
    2 read any value below zero as invalid, so every such plan was a false data-quality REJECT."""
    result = _run(market=_market(funding_8h=funding))
    assert result.status == "APPROVED", result.reasons[-1]


@pytest.mark.parametrize("funding", [float("nan"), float("inf"), None])
def test_gate_2_still_rejects_unknown_funding(funding: float | None) -> None:
    result = _run(market=_market(funding_8h=funding))
    assert result.status == "REJECTED" and _gates(result)[2] == "REJECT"


def test_gate_2_still_rejects_a_negative_spread() -> None:
    result = _run(market=_market(spread_pct=-0.01))
    assert result.status == "REJECTED" and _gates(result)[2] == "REJECT"


def test_no_gate_rule_reads_the_traders_family() -> None:
    """`PlanInput.trader_family` is carried for the journal (the Reviewer avoids that family).

    Which lab wrote a plan is not a reason to size it differently, so no gate may consult it.
    """
    import inspect

    from argus.gate import protections, rules, sizing

    for module in (rules, sizing, protections):
        reads = [
            line.strip()
            for line in inspect.getsource(module).splitlines()
            if ".trader_family" in line and not line.strip().startswith("#")
        ]
        assert not reads, f"{module.__name__} reads the trader family: {reads}"
