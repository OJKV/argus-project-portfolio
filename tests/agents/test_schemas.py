"""The LLM's output contract (LLM_AGENTS.md §2.1).

These validators are the second guardrail after the prompt and before the Risk Gate. They encode
the trading rules that must hold no matter what a model says, so each test here is a specific way
a model could have cost money.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.llm.schemas import (
    FactorScores,
    NoTrade,
    RedTeamVerdict,
    RegimeCall,
    ReviewDecision,
    Target,
    TradePlan,
)

EVIDENCE = {"technical": ["technical.price"], "catalyst": ["catalysts.next"]}


def _plan(**over: object) -> TradePlan:
    base: dict[str, object] = {
        "symbol": "BTC",
        "side": "long",
        "entry_kind": "limit",
        "entry_low": 99.0,
        "entry_high": 101.0,
        "stop": 94.0,
        "targets": [Target(price=110.0, close_pct=50), Target(price=120.0, close_pct=50)],
        "time_stop_hours": 120,
        "scores": FactorScores(fundamental=0, onchain=0, macro=0, catalyst=1, technical=2),
        "evidence": EVIDENCE,
        "thesis": "trend continuation above the 20/50",
    }
    base.update(over)
    return TradePlan(**base)  # type: ignore[arg-type]


def test_a_well_formed_plan_validates() -> None:
    assert _plan().side == "long"


def test_the_model_cannot_set_size_or_leverage() -> None:
    """Sizing is the Risk Gate's job. The fields do not exist, so a model cannot suggest one."""
    assert "size" not in TradePlan.model_fields
    assert "leverage" not in TradePlan.model_fields
    with pytest.raises(ValidationError):
        _plan(leverage=20)


def test_partial_closes_must_add_up_to_the_whole_position() -> None:
    """Targets summing to 80% leave a fifth of the position with no exit plan at all."""
    with pytest.raises(ValidationError, match="100"):
        _plan(targets=[Target(price=110.0, close_pct=50), Target(price=120.0, close_pct=30)])


def test_a_long_stop_must_sit_below_the_entry() -> None:
    with pytest.raises(ValidationError, match="stop"):
        _plan(stop=105.0)


def test_a_short_stop_must_sit_above_the_entry() -> None:
    with pytest.raises(ValidationError, match="stop"):
        _plan(side="short", stop=95.0, targets=[Target(price=90.0, close_pct=100)])


def test_targets_must_lie_in_the_profit_direction() -> None:
    with pytest.raises(ValidationError, match="target"):
        _plan(targets=[Target(price=95.0, close_pct=100)])


def test_targets_must_be_ordered_away_from_the_entry() -> None:
    with pytest.raises(ValidationError, match="order"):
        _plan(targets=[Target(price=120.0, close_pct=50), Target(price=110.0, close_pct=50)])


def test_a_time_stop_cannot_outlive_the_swing_horizon() -> None:
    with pytest.raises(ValidationError):
        _plan(time_stop_hours=400)  # > 168h = a week


def test_prices_must_be_finite_and_positive() -> None:
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            _plan(stop=bad)


def test_the_entry_band_cannot_be_inverted() -> None:
    with pytest.raises(ValidationError):
        _plan(entry_low=101.0, entry_high=99.0)


def test_every_non_zero_score_must_cite_something() -> None:
    """A score with no evidence is an opinion. The gate treats opinions as zero."""
    with pytest.raises(ValidationError, match="evidence"):
        _plan(scores=FactorScores(fundamental=2, onchain=0, macro=0, catalyst=1, technical=2))


def test_a_zero_score_needs_no_citation() -> None:
    plan = _plan(scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=1))
    assert plan.scores.total == 1


def test_scores_stay_inside_the_defined_range() -> None:
    with pytest.raises(ValidationError):
        FactorScores(fundamental=3, onchain=0, macro=0, catalyst=0, technical=0)


def test_no_trade_is_a_first_class_answer() -> None:
    answer = NoTrade(symbol="BTC", reason="chop, no edge")
    assert answer.symbol == "BTC"


def test_a_regime_call_carries_its_reasons() -> None:
    call = RegimeCall(label="TREND_UP", confidence=4, reasons=["price over 20/50", "breadth 60%"])
    assert call.label == "TREND_UP"
    with pytest.raises(ValidationError):
        RegimeCall(label="MOON", confidence=4, reasons=["x"])


def test_a_red_team_verdict_must_name_what_would_break_the_thesis() -> None:
    verdict = RedTeamVerdict(agree=False, severity=3, objections=["funding is crowded long"])
    assert verdict.objections
    with pytest.raises(ValidationError):
        RedTeamVerdict(agree=False, severity=3, objections=[])


def test_the_reviewer_can_only_move_a_stop_towards_profit() -> None:
    """A reviewer that can widen a stop is a reviewer that can turn a loss into a bigger one."""
    assert (
        ReviewDecision(
            action="MOVE_STOP", side="long", current_stop=94.0, new_stop=97.0, rationale="lock in"
        ).new_stop
        == 97.0
    )
    with pytest.raises(ValidationError, match="stop"):
        ReviewDecision(
            action="MOVE_STOP", side="long", current_stop=94.0, new_stop=90.0, rationale="give room"
        )
    with pytest.raises(ValidationError, match="stop"):
        ReviewDecision(
            action="MOVE_STOP", side="short", current_stop=106.0, new_stop=110.0, rationale="room"
        )


def test_the_trader_answers_with_one_shape_not_a_union() -> None:
    """Measured 2026-09-20: a `TradePlan | NoTrade` output type makes pydantic-ai expose two
    tools, and several free models cannot pick between them. Groq's gpt-oss-120b produced a
    perfectly good refusal and then called a tool named `json` that was never offered.

    One tool with a discriminator field is understood by every model we route to.
    """
    from argus.llm.schemas import TraderAnswer

    traded = TraderAnswer(decision="trade", plan=_plan())
    assert traded.plan is not None and traded.reason is None

    refused = TraderAnswer(decision="no_trade", reason="price already at the 20-period high")
    assert refused.plan is None


def test_a_trade_without_a_plan_is_rejected() -> None:
    from argus.llm.schemas import TraderAnswer

    with pytest.raises(ValidationError, match="plan"):
        TraderAnswer(decision="trade", reason="looks good")


def test_a_refusal_must_give_a_reason() -> None:
    from argus.llm.schemas import TraderAnswer

    with pytest.raises(ValidationError, match="reason"):
        TraderAnswer(decision="no_trade")
