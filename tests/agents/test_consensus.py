"""Two traders, different families, compared by code.

The comparison is deliberately not an LLM's job. Asking a model "do these agree?" reintroduces
exactly the judgement the second opinion exists to check, and it would let a persuasive rationale
paper over a direction mismatch.
"""

from __future__ import annotations

from argus.llm.agents.consensus import Agreement, compare
from argus.llm.schemas import FactorScores, Target, TradePlan, TraderAnswer


def _plan(side: str = "long", stop: float = 94.0, entry: float = 100.0, **over) -> TradePlan:
    base = {
        "symbol": "BTC",
        "side": side,
        "entry_kind": "limit",
        "entry_low": entry - 1,
        "entry_high": entry + 1,
        "stop": stop,
        "targets": [Target(price=entry + 12, close_pct=50), Target(price=entry + 24, close_pct=50)]
        if side == "long"
        else [Target(price=entry - 12, close_pct=50), Target(price=entry - 24, close_pct=50)],
        "time_stop_hours": 120,
        "scores": FactorScores(fundamental=0, onchain=0, macro=0, catalyst=1, technical=2),
        "evidence": {"catalyst": ["news.0"], "technical": ["tech.px"]},
    }
    base.update(over)
    return TradePlan(**base)


def _answer(plan: TradePlan | None, reason: str = "no edge") -> TraderAnswer:
    if plan is None:
        return TraderAnswer(decision="no_trade", reason=reason)
    return TraderAnswer(decision="trade", plan=plan)


def test_same_direction_agrees() -> None:
    result = compare(_answer(_plan("long")), _answer(_plan("long")))
    assert result.agreement is Agreement.AGREE
    assert result.tradeable


def test_opposite_directions_are_never_traded() -> None:
    """One model long and another short on the same asset is not a disagreement to average —
    it means at least one of them has misread the dossier."""
    result = compare(_answer(_plan("long")), _answer(_plan("short", stop=106.0)))
    assert result.agreement is Agreement.CONFLICT
    assert not result.tradeable


def test_a_refusal_against_a_plan_blocks_the_trade() -> None:
    """The second opinion declining is a veto, not a tie. A trade needs both to want it."""
    result = compare(_answer(_plan("long")), _answer(None))
    assert result.agreement is Agreement.ONE_SIDED
    assert not result.tradeable


def test_both_refusing_agrees_on_no_trade() -> None:
    result = compare(_answer(None), _answer(None))
    assert result.agreement is Agreement.AGREE
    assert not result.tradeable


def test_the_first_plan_is_the_one_that_gets_traded() -> None:
    """When both agree, the primary Trader's plan is used. Merging two sets of levels would
    produce a plan neither model actually proposed."""
    primary = _plan("long", stop=94.0)
    second = _plan("long", stop=91.0)
    result = compare(_answer(primary), _answer(second))
    assert result.plan is primary


def test_wildly_different_stops_are_flagged_even_when_the_direction_matches() -> None:
    """Same direction, but one risks three times what the other does. That is agreement about
    the idea and disagreement about the trade."""
    result = compare(_answer(_plan("long", stop=97.5)), _answer(_plan("long", stop=88.0)))
    assert result.agreement is Agreement.DIVERGENT
    assert not result.tradeable


def test_similar_stops_still_agree() -> None:
    result = compare(_answer(_plan("long", stop=94.0)), _answer(_plan("long", stop=93.5)))
    assert result.agreement is Agreement.AGREE
    assert result.tradeable


def test_the_reason_is_always_stated() -> None:
    for result in (
        compare(_answer(_plan("long")), _answer(_plan("short", stop=106.0))),
        compare(_answer(_plan("long")), _answer(None)),
        compare(_answer(None), _answer(None)),
    ):
        assert result.detail
