"""The Analyst's golden cases: what is true, scored honestly.

The failure this agent is built to avoid is a confident five-factor reading of a dossier that
only contains three families. A factor with no data scores zero; anything else is a guess wearing
a number.
"""

from __future__ import annotations

import pytest

from argus.llm.golden import ANALYST_CASES, score_analyst
from argus.llm.schemas import AnalystView, FactorScores


def _view(**over) -> AnalystView:
    base = {
        "symbol": "BTC",
        "scores": FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
        "evidence": {"technical": ["tech.ema4h.order"]},
        "confidence": 3,
    }
    base.update(over)
    return AnalystView(**base)


def test_the_set_covers_the_ways_a_reading_goes_wrong() -> None:
    names = {c.name for c in ANALYST_CASES}
    assert {
        "complete_bullish_picture",
        "missing_onchain_family",
        "promotional_news_only",
        "conflicting_signals",
        "stale_prices",
    } <= names


def test_a_factor_without_data_must_score_zero() -> None:
    case = next(c for c in ANALYST_CASES if c.name == "missing_onchain_family")
    honest = _view(
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
        evidence={"technical": ["tech.ema4h.order"]},
    )
    assert score_analyst(case, honest).correct

    guessed = _view(
        scores=FactorScores(fundamental=0, onchain=2, macro=0, catalyst=0, technical=2),
        evidence={"technical": ["tech.ema4h.order"], "onchain": ["chain.tvl"]},
    )
    assert not score_analyst(case, guessed).correct


def test_an_uncited_score_cannot_even_be_constructed() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="evidence"):
        _view(
            scores=FactorScores(fundamental=0, onchain=0, macro=2, catalyst=0, technical=0),
            evidence={},
        )


def test_an_invented_level_is_caught() -> None:
    case = next(c for c in ANALYST_CASES if c.name == "complete_bullish_picture")
    invented = _view(key_levels={"resistance": 999999.0})
    assert score_analyst(case, invented).invented_evidence


def test_a_level_taken_from_the_dossier_is_not_invented() -> None:
    case = next(c for c in ANALYST_CASES if c.name == "complete_bullish_picture")
    real = float(case.dossier().technical["high_20_4h"])
    assert not score_analyst(case, _view(key_levels={"hi20_4h": real})).invented_evidence


@pytest.mark.parametrize("case", ANALYST_CASES, ids=lambda c: c.name)
def test_every_analyst_case_renders(case) -> None:
    from argus.llm.agents.analyst import build_prompt

    text = build_prompt(case.dossier())
    assert case.dossier().symbol in text
    assert "scores 0" in text
