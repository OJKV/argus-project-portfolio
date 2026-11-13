"""One Trader call for the whole cycle. The point is ranking, so that is what gets tested."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from argus.llm.agents.trader_batch import build_prompt, keep_offered, offered_symbols
from argus.llm.golden import CASES
from argus.llm.schemas import MAX_PLANS_PER_RUN, FactorScores, Target, TradePlan, TraderBatch

PORTFOLIO = {"equity_usd": 100.0, "open_positions": [], "open_risk_pct": 0.0}


def _dossiers(n: int = 4):  # noqa: ANN202 - fixtures; the type is a list of dossiers
    return [c.dossier() for c in CASES[:n]]


def _plan(symbol: str) -> TradePlan:
    return TradePlan(
        symbol=symbol,
        side="long",
        entry_kind="limit",
        entry_low=99.8,
        entry_high=100.2,
        stop=96.0,
        targets=[Target(price=110.0, close_pct=100)],
        time_stop_hours=96,
        evidence={"technical": ["tech.px"]},
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
    )


def test_more_than_two_plans_cannot_be_expressed() -> None:
    """A cap in a prompt is a request; a cap in a validator is a refusal."""
    with pytest.raises(ValidationError):
        TraderBatch(plans=[_plan(s) for s in ("BTC", "ETH", "SOL")])


def test_two_plans_on_one_symbol_are_refused() -> None:
    """That is one position with two opinions about it."""
    with pytest.raises(ValidationError):
        TraderBatch(plans=[_plan("BTC"), _plan("BTC")])


def test_an_empty_answer_must_still_be_an_answer() -> None:
    with pytest.raises(ValidationError):
        TraderBatch()
    assert TraderBatch(no_trade_reason="nothing cleared the bar").plans == []


def test_a_plan_on_an_unoffered_symbol_is_dropped() -> None:
    """No dossier behind it means nothing downstream could gate it honestly."""
    dossiers = _dossiers()
    answer = TraderBatch(plans=[_plan(dossiers[0].symbol), _plan("DOGE")])
    kept, invented = keep_offered(answer, offered_symbols(dossiers))
    assert [p.symbol for p in kept] == [dossiers[0].symbol]
    assert invented == ["DOGE"]


def test_every_candidate_reaches_the_prompt_once() -> None:
    dossiers = _dossiers(5)
    prompt = build_prompt(dossiers, PORTFOLIO)
    for d in dossiers:
        assert f'<candidate symbol="{d.symbol}"' in prompt
    assert prompt.count("<candidate ") == len(dossiers)


def test_the_prompt_carries_the_book_and_the_cap() -> None:
    prompt = build_prompt(_dossiers(), {"equity_usd": 100.0, "open_positions": [{"symbol": "BTC"}]})
    # Without the open book, "does this double an exposure" is unanswerable.
    assert "open_positions" in prompt and "BTC" in prompt
    assert str(MAX_PLANS_PER_RUN) in prompt


def test_batching_is_far_cheaper_than_asking_per_asset() -> None:
    """The template is re-sent with every dossier on the per-asset path (F-117)."""
    from argus.llm.agents.trader import build_prompt as single

    dossiers = _dossiers(6)
    batched = len(build_prompt(dossiers, PORTFOLIO))
    per_asset = sum(len(single(d)) for d in dossiers)
    assert batched < per_asset / 2


def test_an_empty_candidate_set_still_renders() -> None:
    """Every asset failing research is a real Tuesday, not a crash."""
    prompt = build_prompt([], PORTFOLIO)
    assert "no candidates cleared research" in prompt
