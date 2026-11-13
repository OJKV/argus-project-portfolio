"""The chain between a plan and the gate. Its job is to say no, so that is what is tested."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from argus.llm.agents.consensus import Agreement
from argus.llm.golden import CASES
from argus.llm.router import AllModelsFailedError
from argus.llm.schemas import FactorScores, Target, TradePlan, TraderAnswer
from argus.runs.pipeline import vet

PORTFOLIO: dict[str, Any] = {"equity": 100.0, "positions": [], "open_risk_pct": 0.0}


def _plan(side: str = "long", stop: float = 96.0) -> TradePlan:
    # Targets follow the side: a short's target sits below the entry, and the schema enforces it.
    target = 110.0 if side == "long" else 90.0
    return TradePlan(
        symbol="BTC",
        side=side,  # type: ignore[arg-type]
        entry_kind="limit",
        entry_low=99.8,
        entry_high=100.2,
        stop=stop,
        targets=[Target(price=target, close_pct=100)],
        time_stop_hours=96,
        thesis="EMA reclaim",
        evidence={"technical": ["tech.px"]},
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
    )


def _trade(side: str = "long", stop: float = 96.0) -> TraderAnswer:
    return TraderAnswer(decision="trade", plan=_plan(side, stop))


def _refusal() -> TraderAnswer:
    return TraderAnswer(decision="no_trade", reason="no edge")


class _Result:
    def __init__(self, output: Any) -> None:
        self.output = output


class FakeRouter:
    """Stands in for the router so the chain's decisions are testable without a network."""

    def __init__(self, second: Any = None, verdict: Any = None) -> None:
        self._second, self._verdict = second, verdict
        self.called: list[str] = []


@pytest.fixture
def dossier():  # noqa: ANN201 - a fixture; the type is the dossier
    return next(c for c in CASES if c.name == "clean_uptrend").dossier()


def _red(severity: int, agree: bool = False) -> Any:
    from argus.llm.schemas import RedTeamVerdict

    return RedTeamVerdict(agree=agree, severity=severity, objections=["funding at p96 unmentioned"])


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    second: Any = None,
    verdict: Any = None,
    red: Any = None,
) -> list[str]:
    from argus.llm.agents import consensus as consensus_agent
    from argus.llm.agents import redteam as red_agent
    from argus.llm.agents import riskdebate as risk_agent

    seen: list[str] = []
    # A benign default so tests about the other checks are not about the red team. Note
    # agree=False: measured near-constant (F-136), so only severity may move the decision.
    red = _red(1) if red is None else red

    def fake_red(*_a: Any, **_k: Any) -> Any:
        seen.append("redteam")
        if isinstance(red, Exception):
            raise red
        return _Result(red)

    monkeypatch.setattr(red_agent, "challenge_plan", fake_red)

    def fake_second(*_a: Any, **_k: Any) -> Any:
        seen.append("consensus")
        if isinstance(second, Exception):
            raise second
        return _Result(second)

    def fake_debate(*_a: Any, **_k: Any) -> Any:
        seen.append("riskdebate")
        if isinstance(verdict, Exception):
            raise verdict
        return _Result(verdict)

    monkeypatch.setattr(consensus_agent, "second_opinion", fake_second)
    monkeypatch.setattr(risk_agent, "debate", fake_debate)
    return seen


def _verdict(recommendation: str, concerns: list[str] | None = None) -> Any:
    from argus.llm.schemas import RiskVerdict

    return RiskVerdict(
        recommendation=recommendation,  # type: ignore[arg-type]
        concerns=concerns or (["heat"] if recommendation != "proceed" else []),
        rationale="because",
    )


def test_a_refusal_never_reaches_the_checks(monkeypatch: pytest.MonkeyPatch, dossier) -> None:  # noqa: ANN001
    seen = _patch(monkeypatch)
    result = vet(FakeRouter(), "r1", dossier, _refusal(), "google", PORTFOLIO, "TREND_UP")
    assert not result.approved
    assert result.blocked_by == "trader"
    # No point paying for a second opinion on a trade nobody proposed.
    assert seen == []


def test_agreement_and_proceed_lets_the_plan_through(
    monkeypatch: pytest.MonkeyPatch, dossier
) -> None:  # noqa: ANN001
    seen = _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert result.approved
    assert result.size_factor == 1.0
    assert seen == ["consensus", "redteam", "riskdebate"]
    assert ("consensus", Agreement.AGREE.value) in result.checks


def test_a_second_opinion_that_declines_drops_the_trade(
    monkeypatch: pytest.MonkeyPatch, dossier
) -> None:  # noqa: ANN001
    seen = _patch(monkeypatch, second=_refusal(), verdict=_verdict("proceed"))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert not result.approved
    assert result.blocked_by == "consensus"
    # A veto, not a vote — and the risk debate is never asked about a trade already dead.
    assert "riskdebate" not in seen


def test_opposite_directions_drop_the_trade(monkeypatch: pytest.MonkeyPatch, dossier) -> None:  # noqa: ANN001
    _patch(monkeypatch, second=_trade(side="short", stop=104.0), verdict=_verdict("proceed"))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert result.blocked_by == "consensus"
    assert ("consensus", Agreement.CONFLICT.value) in result.checks


def test_risk_debate_can_halve_but_never_raise(monkeypatch: pytest.MonkeyPatch, dossier) -> None:  # noqa: ANN001
    _patch(monkeypatch, second=_trade(), verdict=_verdict("reduce"))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert result.approved
    assert result.size_factor == 0.5

    _patch(monkeypatch, second=_trade(), verdict=_verdict("decline"))
    declined = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert not declined.approved
    assert declined.blocked_by == "riskdebate"
    assert declined.size_factor == 0.0


@pytest.mark.parametrize("failing", ["consensus", "redteam", "riskdebate"])
def test_an_unavailable_safety_agent_blocks_the_trade(
    monkeypatch: pytest.MonkeyPatch, dossier, failing: str
) -> None:  # noqa: ANN001
    """Silence from an agent whose job is to object is not approval.

    This is what `safety` criticality means, and it is the opposite of `optional`: trading anyway
    would quietly turn an unavailable check into a passed one.
    """
    boom = AllModelsFailedError("every model failed")
    kwargs: dict[str, Any] = {
        "consensus": {"second": boom},
        "redteam": {"second": _trade(), "red": boom},
        "riskdebate": {"second": _trade(), "verdict": boom},
    }[failing]
    _patch(monkeypatch, **kwargs)  # type: ignore[arg-type]
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert not result.approved
    assert result.blocked_by == failing
    assert any("unavailable" in outcome for _, outcome in result.checks)


def test_the_second_opinion_is_asked_on_another_family(
    monkeypatch: pytest.MonkeyPatch, dossier
) -> None:  # noqa: ANN001
    from argus.llm.agents import consensus as consensus_agent

    captured: dict[str, Any] = {}

    def fake_second(_router: Any, _run: str, _d: Any, trader_family: str, **_k: Any) -> Any:
        captured["family"] = trader_family
        return _Result(_trade())

    monkeypatch.setattr(consensus_agent, "second_opinion", fake_second)
    monkeypatch.setattr(
        "argus.llm.agents.riskdebate.debate", lambda *a, **k: _Result(_verdict("proceed"))
    )
    monkeypatch.setattr("argus.llm.agents.redteam.challenge_plan", lambda *a, **k: _Result(_red(1)))
    vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    # Passed through so the router can EXCLUDE it: agreement between siblings is not evidence.
    assert captured["family"] == "google"


def test_every_decision_is_journallable(monkeypatch: pytest.MonkeyPatch, dossier) -> None:  # noqa: ANN001
    _patch(monkeypatch, second=_trade(), verdict=_verdict("reduce"))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    # A size that shrank for a reason nobody recorded cannot be reviewed or learned from.
    assert [agent for agent, _ in result.checks] == ["consensus", "redteam", "riskdebate"]
    assert all(outcome for _, outcome in result.checks)


def test_a_passed_plan_is_journaled_with_its_factor(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    """Passes are recorded too: a block rate needs the plans that were not blocked."""
    import json

    from sqlalchemy import text

    from argus.journal.db import open_journal, start_run

    engine = open_journal(tmp_path / "journal.db")
    _patch(monkeypatch, second=_trade(), verdict=_verdict("reduce"))
    with start_run(engine, "manager", "cfg") as run_id:
        vet(
            FakeRouter(), run_id, dossier, _trade(), "google", PORTFOLIO, "TREND_UP", journal=engine
        )
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM vettings")).mappings().one()
    assert row["blocked_by"] is None
    assert row["size_factor"] == 0.5
    assert [agent for agent, _ in json.loads(row["checks"])] == [
        "consensus",
        "redteam",
        "riskdebate",
    ]
    assert json.loads(row["unsupported"]) == {}


def test_the_gate_halves_the_risk_when_the_chain_says_reduce() -> None:
    """The factor has to survive the trip into sizing, or the check is decorative."""
    from argus.gate.sizing import size_position
    from tests.gate.test_sizing import _inp

    base = _inp(1000.0)
    full = size_position(base)
    half = size_position(dataclasses.replace(base, agent_mult=0.5))
    # Placed risk (G-11) carries lot rounding: half the contracts, within one lot of exactly half.
    assert half.risk_usd == pytest.approx(full.risk_usd / 2, rel=0.05)


def test_no_agent_factor_can_increase_risk() -> None:
    """Clamped in code, not trusted from the caller. There is no value meaning "more"."""
    from argus.gate.sizing import size_position
    from tests.gate.test_sizing import _inp

    base = _inp(1000.0)
    full = size_position(base)
    for cheeky in (1.5, 10.0, float("inf")):
        assert size_position(
            dataclasses.replace(base, agent_mult=cheeky)
        ).risk_usd == pytest.approx(full.risk_usd)


def test_the_red_team_is_judged_on_severity_never_on_agree(
    monkeypatch: pytest.MonkeyPatch, dossier
) -> None:  # noqa: ANN001
    """F-136: `agree` is False on sound readings rated 2/5. Acting on it would veto everything."""
    _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"), red=_red(2, agree=False))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert result.approved
    assert result.size_factor == 1.0


def test_an_actionable_objection_halves_the_trade(monkeypatch: pytest.MonkeyPatch, dossier) -> None:  # noqa: ANN001
    _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"), red=_red(3))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert result.approved
    assert result.size_factor == 0.5


def test_a_reading_that_cannot_be_relied_on_drops_the_trade(
    monkeypatch: pytest.MonkeyPatch, dossier
) -> None:  # noqa: ANN001
    seen = _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"), red=_red(4))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert not result.approved
    assert result.blocked_by == "redteam"
    assert "riskdebate" not in seen


def test_the_red_team_and_risk_debate_compound_and_only_shrink(
    monkeypatch: pytest.MonkeyPatch, dossier
) -> None:  # noqa: ANN001
    _patch(monkeypatch, second=_trade(), verdict=_verdict("reduce"), red=_red(3))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert result.size_factor == 0.25


def test_the_red_team_attacks_on_another_family_than_the_trader(
    monkeypatch: pytest.MonkeyPatch, dossier
) -> None:  # noqa: ANN001
    captured: dict[str, Any] = {}

    def fake_red(_router: Any, _run: str, _d: Any, _plan: Any, trader_family: str) -> Any:
        captured["family"] = trader_family
        return _Result(_red(1))

    _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"))
    monkeypatch.setattr("argus.llm.agents.redteam.challenge_plan", fake_red)
    vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert captured["family"] == "google"
