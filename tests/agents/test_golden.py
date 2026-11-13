"""The golden set: fixed dossiers with known-correct answers.

Every model faces the identical question, so differences in the answers are differences in the
models. Without this, "which model should be the Red Team?" is settled by taste.
"""

from __future__ import annotations

import pytest

from argus.llm.golden import CASES, Expectation, evidence_paths, score_answer
from argus.llm.schemas import FactorScores, Target, TradePlan, TraderAnswer


def test_the_set_covers_the_cases_that_matter() -> None:
    names = {case.name for case in CASES}
    # Each of these is a way a model can be wrong that costs money.
    assert {
        "clean_uptrend",
        "chop_no_edge",
        "stale_data",
        "counter_regime_short",
        "already_extended",
        "wide_spread",
    } <= names


def test_every_case_states_what_a_good_answer_looks_like() -> None:
    for case in CASES:
        assert case.expect in (Expectation.TRADE, Expectation.NO_TRADE), case.name
        assert case.why, f"{case.name} must say why, or the score is unarguable"
        assert case.dossier().symbol


def test_evidence_paths_are_extracted_from_a_rendered_dossier() -> None:
    case = next(c for c in CASES if c.name == "clean_uptrend")
    paths = evidence_paths(case.dossier())
    assert paths, "a dossier with data must expose citable paths"
    assert any("tech" in p for p in paths)


def _answer(decision: str, evidence: dict[str, list[str]] | None = None) -> TraderAnswer:
    if decision == "no_trade":
        return TraderAnswer(decision="no_trade", reason="no edge")
    plan = TradePlan(
        symbol="BTC",
        side="long",
        entry_kind="limit",
        entry_low=99.0,
        entry_high=101.0,
        stop=94.0,
        targets=[Target(price=110.0, close_pct=50), Target(price=120.0, close_pct=50)],
        time_stop_hours=120,
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
        evidence=evidence or {"technical": ["technical.price"]},
    )
    return TraderAnswer(decision="trade", plan=plan)


def _real_path(case) -> str:
    """A citation the dossier actually supports, taken from the rendering the model sees."""
    return next(p for p in sorted(evidence_paths(case.dossier())) if p.startswith("tech"))


def test_a_correct_decision_scores_and_a_wrong_one_does_not() -> None:
    case = next(c for c in CASES if c.expect is Expectation.NO_TRADE)
    assert score_answer(case, _answer("no_trade")).correct
    assert not score_answer(case, _answer("trade")).correct


def test_invented_evidence_is_caught() -> None:
    """A model citing a field that is not in the dossier is worse than one citing nothing: it
    looks rigorous while being fabricated."""
    case = next(c for c in CASES if c.expect is Expectation.TRADE)
    fabricated = _answer("trade", {"technical": ["technical.rsi_from_thin_air"]})
    assert score_answer(case, fabricated).invented_evidence

    honest = _answer("trade", {"technical": [f"{_real_path(case)}=100.0"]})
    assert not score_answer(case, honest).invented_evidence, "quoting a value must not count"


def test_a_refusal_carries_no_evidence_penalty() -> None:
    case = next(c for c in CASES if c.expect is Expectation.NO_TRADE)
    assert not score_answer(case, _answer("no_trade")).invented_evidence


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_every_golden_dossier_renders(case) -> None:
    from argus.features.dossier import render

    text = render(case.dossier())
    assert case.dossier().symbol in text


def test_a_list_index_citation_is_honest_not_invented() -> None:
    """`news.0` means "the first headline" — that is precision, not fabrication.

    The first scorer took the last path segment, saw `0`, and called it invented. On the golden
    set that produced "4 invented citations" for the model with the best decision accuracy, and
    very nearly got it dropped (2026-09-20).
    """
    case = next(c for c in CASES if c.name == "macro_tailwind_long")
    # Every one of these exists in that dossier; `cat.1` deliberately does not, because the
    # catalyst matcher drops entries outside the symbol/date window.
    for citation in ("news.0", "eq.0.metric", "eq.1.value"):
        answer = _answer("trade", {"technical": [citation]})
        assert not score_answer(case, answer).invented_evidence, citation


def test_a_fabricated_path_is_still_caught_after_index_stripping() -> None:
    case = next(c for c in CASES if c.name == "macro_tailwind_long")
    for citation in ("news.0.sentiment_score", "tech.rsi_from_thin_air", "eq.2.made_up"):
        answer = _answer("trade", {"technical": [citation]})
        assert score_answer(case, answer).invented_evidence, citation


def test_source_dot_metric_citations_are_recognised() -> None:
    """The cross-market block stores data as {"src": "fred", "metric": "policy_rate_pct"}, so
    `fred.policy_rate_pct` names something real — arguably more clearly than `eq.3.value`.

    Counting only dict KEYS as citable made that read as fabrication, which was the last of the
    three false accusations this scorer levelled at the most accurate model (2026-09-20).
    """
    case = next(c for c in CASES if c.name == "macro_tailwind_long")
    for citation in ("fred.policy_rate_pct", "cmc.market_cap_usd", "fear_greed.index"):
        answer = _answer("trade", {"technical": [citation]})
        assert not score_answer(case, answer).invented_evidence, citation


def test_an_identifier_that_appears_nowhere_is_still_caught() -> None:
    case = next(c for c in CASES if c.name == "macro_tailwind_long")
    for citation in ("fred.unemployment_rate", "glassnode.sopr", "tech.made_up_metric"):
        answer = _answer("trade", {"technical": [citation]})
        assert score_answer(case, answer).invented_evidence, citation


def test_an_eval_is_recorded_so_drift_is_visible(tmp_path) -> None:
    """One score is a snapshot; the useful signal is a model that held the shape last week and
    fails it today, because a free tier changed underneath us."""
    import json

    from sqlalchemy import text

    from argus.journal.db import open_journal
    from argus.llm.golden import ModelReport, record_eval

    engine = open_journal(tmp_path / "journal.db")
    report = ModelReport(label="gemini/gemini-3.5-flash-lite", correct=8, total=10, invented=0)
    report.latencies_ms = [1000, 2000, 3000]
    report.notes = ["chop_no_edge: said trade"]
    record_eval(engine, report, tier="fast")

    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM model_evals")).mappings().one()
    assert row["provider"] == "gemini"
    assert row["model"] == "gemini-3.5-flash-lite"
    assert row["correct"] == 8 and row["cases"] == 10
    assert row["p50_ms"] == 2000
    assert row["usable"] == 1
    assert json.loads(row["notes"]) == ["chop_no_edge: said trade"]


def test_a_named_gap_is_citable_evidence() -> None:
    """Refusing to trade because data is missing is behaviour to reward, not to score as fraud.

    The dossier renders gaps as sentences — `"gaps":["onchain: missing"]` — so a model citing
    `gaps.onchain` points at something the prompt really contains. Counting only the `gaps` key
    made that read as fabrication (F-116).
    """
    from argus.llm.golden import CASES, _leaf, evidence_paths

    case = next(c for c in CASES if c.name == "counter_regime_short")
    paths = evidence_paths(case.dossier())
    assert _leaf("gaps.onchain") in paths
    assert _leaf("gaps.news") in paths
    # The vocabulary still has edges: a gap nobody reported is not citable.
    assert _leaf("gaps.invented_feed") not in paths
