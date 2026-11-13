"""The demo exploration profile (owner's grilling decisions, 2026-10-26).

Two things are proved here, rule by rule: every relaxed value applies on the demo account, and the
same plan that passes on demo still fails on live — so live is provably unchanged.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from argus.features.regime import RegimeInputs, classify
from argus.gate import protections, rules
from argus.gate.limits import (
    DEMO_EXPLORATION,
    DEMO_PROFILE,
    LIVE_CONSERVATIVE,
    DecisionLimits,
    limits_for,
)
from argus.gate.protections import check_protections
from argus.gate.rules import OpenPosition, Target, evaluate
from argus.journal.db import open_journal
from argus.llm.router import AllModelsFailedError
from argus.runs.manager import trading_history
from argus.runs.pipeline import vet
from tests.gate.test_rules import NOW, STEPS, _history, _market, _plan, _portfolio, _scores
from tests.runs.test_manager_book import _closed_trade
from tests.runs.test_pipeline import FakeRouter, _patch, _red, _trade, _verdict

DEMO, LIVE = DEMO_EXPLORATION, LIVE_CONSERVATIVE


# --- the profile itself -----------------------------------------------------------------------


def test_the_demo_profile_carries_the_approved_values() -> None:
    assert (DEMO.min_score, DEMO.min_score_counter_regime) == (2, 4)
    assert (DEMO.min_rr_tp1, DEMO.min_rr_blended) == (0.5, 0.8)
    assert (DEMO.max_entries_per_run, DEMO.max_entries_per_day) == (3, 8)
    assert DEMO.max_positions == 6
    assert DEMO.max_open_risk_pct == 8.0
    assert DEMO.chop_limit_only is False
    assert DEMO.redteam_drop_severity == 5
    assert DEMO.consensus_outage_proceeds is True
    assert DEMO.riskdebate_decline_factor == 0.5
    assert DEMO.consecutive_loss_pause == 5


def test_live_keeps_every_constant_the_gate_has_always_enforced() -> None:
    assert (LIVE.min_score, rules.MIN_SCORE) == (4, 4)
    assert (LIVE.min_score_counter_regime, rules.MIN_SCORE_COUNTER_REGIME) == (6, 6)
    assert (LIVE.min_rr_tp1, rules.MIN_RR_TP1) == (1.5, 1.5)
    assert (LIVE.min_rr_blended, rules.MIN_RR_BLENDED) == (2.0, 2.0)
    assert (LIVE.max_open_risk_pct, rules.MAX_OPEN_RISK_PCT) == (5.0, 5.0)
    assert (LIVE.max_entries_per_run, protections.MAX_ENTRIES_PER_RUN) == (2, 2)
    assert (LIVE.max_entries_per_day, protections.MAX_ENTRIES_PER_DAY) == (3, 3)
    assert (LIVE.consecutive_loss_pause, protections.CONSECUTIVE_LOSS_LIMIT) == (3, 3)
    assert LIVE.max_positions is None  # the risk profile's cap (4, or 2 under $500), as before
    assert LIVE.chop_limit_only is True
    assert LIVE.redteam_drop_severity == 4
    assert LIVE.consensus_outage_proceeds is False
    assert LIVE.riskdebate_decline_factor == 0.0


def test_only_the_exact_demo_venue_relaxes_anything() -> None:
    assert limits_for("demo") is DEMO_PROFILE is DEMO
    for venue in ("live", "LIVE", "Demo", "", "demo ", "paper"):
        assert limits_for(venue) is LIVE


def test_the_venue_comes_from_settings_alone() -> None:
    """Non-negotiable #3: the profile follows `OKX_DEMO`, nothing an agent writes."""
    from argus.config import Settings

    assert Settings(okx_demo=True).limits is DEMO
    live = Settings(
        okx_demo=False,
        mode="auto",
        live_trading="I_UNDERSTAND",
        live_venue="I_UNDERSTAND_REAL_MONEY",
    )
    assert live.limits is LIVE


def test_no_llm_output_can_carry_or_change_the_profile() -> None:
    from pydantic import ValidationError

    from argus.llm.schemas import TraderAnswer, TraderBatch

    plan = _trade().plan
    assert plan is not None
    for key, value in (("venue", "demo"), ("limits", "demo_exploration"), ("profile", "demo")):
        payload = plan.model_dump(mode="json") | {key: value}
        with pytest.raises(ValidationError):
            TraderAnswer.model_validate({"decision": "trade", "plan": payload})
        with pytest.raises(ValidationError):
            TraderBatch.model_validate({"plans": [payload]})
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEMO.min_score = 0  # type: ignore[misc]


# --- the gate: a plan that passes on demo fails on live, rule by rule --------------------------


def _chop(limits: DecisionLimits) -> Any:
    inputs = RegimeInputs(
        btc_close=100,
        btc_ema20=95,
        btc_ema50=90,
        btc_ema20_slope=0.5,
        btc_rv20_pct=40,
        breadth_20=50,
        next_event_hours=72,
    )
    return classify(inputs, limits)


def _trend_down() -> Any:
    inputs = RegimeInputs(
        btc_close=90,
        btc_ema20=95,
        btc_ema50=100,
        btc_ema20_slope=-0.5,
        btc_rv20_pct=40,
        breadth_20=30,
        next_event_hours=72,
    )
    return classify(inputs)


def _small(n: int, risk: float = 5.0) -> list[OpenPosition]:
    return [
        OpenPosition(f"A{i}", "short", notional_usd=100.0, risk_usd=risk, beta_btc=0.5)
        for i in range(n)
    ]


# rule -> (gate the live run rejects at, builder(limits) -> (plan, market, portfolio))
GATE_RULES: dict[str, tuple[int, Any]] = {
    "Q1 score 3 (demo min 2, live 4)": (
        1,
        lambda lim: (_plan(scores=_scores(fundamental=0, onchain=0)), _market(), _portfolio()),
    ),
    "Q1 counter-regime score 5 (demo min 4, live 6)": (
        1,
        lambda lim: (_plan(), _market(regime=_trend_down()), _portfolio()),
    ),
    "Q2 TP1 1.2R (demo 1.0R, live 1.5R)": (
        3,
        lambda lim: (
            _plan(targets=[Target(107.2, 50), Target(117.4, 50)]),
            _market(),
            _portfolio(),
        ),
    ),
    "Q2 owner 2026-10-26: TP1 0.6R, blended 0.88R (demo 0.5/0.8R, live 1.5/2.0R)": (
        3,
        lambda lim: (
            _plan(targets=[Target(103.6, 50), Target(107.0, 50)]),
            _market(),
            _portfolio(),
        ),
    ),
    "Q2 blended 1.65R (demo 1.3R, live 2.0R)": (
        3,
        lambda lim: (
            _plan(targets=[Target(109.0, 50), Target(110.8, 50)]),
            _market(),
            _portfolio(),
        ),
    ),
    "Q3 third entry this run (demo 3, live 2)": (
        9,
        lambda lim: (_plan(), _market(), _portfolio(history=_history(entries_this_run=2))),
    ),
    "Q3 sixth entry today (demo 8, live 3)": (
        9,
        lambda lim: (_plan(), _market(), _portfolio(history=_history(entries_today=5))),
    ),
    "Q3 fifth open position (demo 6, live 4)": (
        6,
        lambda lim: (_plan(), _market(), _portfolio(positions=_small(4))),
    ),
    "Q3 open risk 7% (demo 8%, live 5%)": (
        6,
        lambda lim: (_plan(), _market(), _portfolio(positions=_small(2, risk=60.0))),
    ),
    "Q4 market entry in CHOP (demo allowed, live limit-only)": (
        8,
        lambda lim: (
            _plan(entry_kind="market", urgent=True),
            _market(regime=_chop(lim)),
            _portfolio(),
        ),
    ),
    "Q7 three losses in a row (demo pauses at 5, live at 3)": (
        9,
        lambda lim: (
            _plan(),
            _market(),
            _portfolio(
                history=_history(
                    consecutive_losses=3,
                    last_loss_at=NOW - timedelta(hours=1),
                    trades_since_pause=0,
                )
            ),
        ),
    ),
}


def _gate(limits: DecisionLimits, rule: str) -> Any:
    plan, market, portfolio = GATE_RULES[rule][1](limits)
    return evaluate(plan, market, portfolio, STEPS, NOW, limits=limits)


@pytest.mark.parametrize("rule", list(GATE_RULES))
def test_a_plan_that_passes_on_demo_fails_on_live(rule: str) -> None:
    demo = _gate(DEMO, rule)
    assert demo.status in {"APPROVED", "ADJUSTED"}, [
        (r.gate, r.verdict, r.detail) for r in demo.reasons
    ]
    live = _gate(LIVE, rule)
    assert live.status == "REJECTED"
    rejected = [r for r in live.reasons if r.verdict == "REJECT"]
    assert [r.gate for r in rejected] == [GATE_RULES[rule][0]], [
        (r.gate, r.detail) for r in rejected
    ]


def test_the_gate_defaults_to_the_live_profile() -> None:
    """Every caller that does not say otherwise gets today's gate."""
    plan, market, portfolio = GATE_RULES["Q1 score 3 (demo min 2, live 4)"][1](LIVE)
    assert evaluate(plan, market, portfolio, STEPS, NOW).status == "REJECTED"


# --- demo's own limits still bite ---------------------------------------------------------


def test_demo_still_rejects_below_its_own_score_and_rr() -> None:
    weak = _plan(scores=_scores(fundamental=0, onchain=0, catalyst=0, technical=1))
    assert evaluate(weak, _market(), _portfolio(), STEPS, NOW, limits=DEMO).status == "REJECTED"
    thin = _plan(targets=[Target(102.4, 50), Target(103.6, 50)])  # TP1 0.4R, blended 0.5R
    assert evaluate(thin, _market(), _portfolio(), STEPS, NOW, limits=DEMO).status == "REJECTED"


@pytest.mark.parametrize(
    "portfolio",
    [
        _portfolio(history=_history(entries_this_run=3)),
        _portfolio(history=_history(entries_today=8)),
        _portfolio(positions=_small(6)),
        _portfolio(positions=_small(2, risk=75.0)),  # 7.5% + this trade's 1% > 8%
    ],
    ids=["4th entry this run", "9th entry today", "7th position", "open risk over 8%"],
)
def test_demo_still_rejects_past_its_own_caps(portfolio: Any) -> None:
    assert evaluate(_plan(), _market(), portfolio, STEPS, NOW, limits=DEMO).status == "REJECTED"


def test_demo_caps_positions_at_six_even_where_the_profile_would_say_two() -> None:
    """The aggressive profile's "two under $500" is a live cap; demo's six replaces it."""
    portfolio = _portfolio(positions=_small(3, risk=1.0))
    assert evaluate(_plan(), _market(), portfolio, STEPS, NOW, max_positions=2).status == (
        "REJECTED"
    )
    demo = evaluate(_plan(), _market(), portfolio, STEPS, NOW, max_positions=2, limits=DEMO)
    assert demo.status in {"APPROVED", "ADJUSTED"}


def test_chop_keeps_its_half_size_on_demo_and_event_risk_stays_limit_only() -> None:
    chop = _chop(DEMO)
    assert chop.label == "CHOP" and chop.limit_only is False and chop.risk_mult == 0.5
    assert _chop(LIVE).limit_only is True
    event = classify(
        RegimeInputs(
            btc_close=100,
            btc_ema20=95,
            btc_ema50=90,
            btc_ema20_slope=0.5,
            btc_rv20_pct=95,
            breadth_20=60,
            next_event_hours=72,
        ),
        DEMO,
    )
    assert event.label == "EVENT_RISK" and event.limit_only is True


def test_a_live_gate_refuses_a_chop_market_entry_even_from_a_demo_regime_view() -> None:
    """Belt and braces: the live gate does not trust a view classified on demo terms."""
    market = _market(regime=_chop(DEMO))
    result = evaluate(_plan(entry_kind="market", urgent=True), market, _portfolio(), STEPS, NOW)
    assert result.status == "REJECTED"
    assert [r.gate for r in result.reasons if r.verdict == "REJECT"] == [8]


# --- Q7: consecutive losses, through the journal ------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.mark.parametrize(("losses", "paused"), [(3, False), (4, False), (5, True)])
def test_demo_pauses_at_five_consecutive_losses_not_three(
    engine: Engine, losses: int, paused: bool
) -> None:
    for i in range(losses):
        at = NOW - timedelta(hours=losses - i)
        _closed_trade(engine, "ETH", "-5", at.isoformat())
    history = trading_history(engine, 2000.0, NOW, DEMO)
    assert history.consecutive_losses == losses
    state = check_protections("BTC", history, NOW, DEMO)
    assert state.allowed is not paused, state.reasons
    # No reduced-size period either before the pause has ever happened.
    if not paused:
        assert history.pause_started_at is None
        assert state.size_mult == 1.0


def test_live_still_pauses_at_three_through_the_journal(engine: Engine) -> None:
    for i in range(3):
        _closed_trade(engine, "ETH", "-5", (NOW - timedelta(hours=3 - i)).isoformat())
    history = trading_history(engine, 2000.0, NOW)
    assert not check_protections("BTC", history, NOW).allowed


# --- the adversarial chain ----------------------------------------------------------------

PORTFOLIO: dict[str, Any] = {"equity": 100.0, "positions": [], "open_risk_pct": 0.0}


@pytest.fixture
def dossier():  # noqa: ANN201
    from argus.llm.golden import CASES

    return next(c for c in CASES if c.name == "clean_uptrend").dossier()


CHAIN_RULES: dict[str, tuple[dict[str, Any], str, float]] = {
    # rule -> (patches, live blocker, demo size factor)
    "Q5 red team severity 4": (
        {"second": _trade(), "verdict": _verdict("proceed"), "red": _red(4)},
        "redteam",
        0.5,
    ),
    "Q5 consensus unavailable": (
        {"second": AllModelsFailedError("every model failed"), "verdict": _verdict("proceed")},
        "consensus",
        1.0,
    ),
    "Q5 risk debate declines": (
        {"second": _trade(), "verdict": _verdict("decline")},
        "riskdebate",
        0.5,
    ),
}


@pytest.mark.parametrize("rule", list(CHAIN_RULES))
def test_a_plan_the_chain_lets_through_on_demo_is_dropped_on_live(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
    rule: str,
) -> None:
    patches, blocker, factor = CHAIN_RULES[rule]
    _patch(monkeypatch, **patches)
    demo = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP", limits=DEMO)
    assert demo.approved, demo.checks
    assert demo.size_factor == factor

    _patch(monkeypatch, **patches)
    live = vet(FakeRouter(), "r1", dossier, _trade(), "google", PORTFOLIO, "TREND_UP")
    assert not live.approved
    assert live.blocked_by == blocker


def test_demo_red_team_severity_three_still_halves_and_five_still_drops(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
) -> None:
    _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"), red=_red(3))
    three = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP", limits=DEMO)
    assert three.approved and three.size_factor == 0.5
    _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"), red=_red(5))
    five = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP", limits=DEMO)
    # owner 2026-10-27: severity 5 shrinks to a quarter on demo instead of dropping the plan.
    assert five.approved and five.size_factor == 0.25
    _patch(monkeypatch, second=_trade(), verdict=_verdict("proceed"), red=_red(5))
    live = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP")
    assert not live.approved and live.blocked_by == "redteam"


def test_demo_consensus_outage_is_recorded_as_such(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
) -> None:
    seen = _patch(
        monkeypatch, second=AllModelsFailedError("every model failed"), verdict=_verdict("proceed")
    )
    result = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP", limits=DEMO)
    assert result.approved
    assert seen == ["consensus", "redteam", "riskdebate"]
    assert any(a == "consensus" and "unavailable" in o for a, o in result.checks)


@pytest.mark.parametrize(
    "second",
    [_trade(side="short", stop=104.0), _trade(stop=90.0)],
    ids=["side mismatch", "stop divergence"],
)
def test_a_real_consensus_disagreement_still_vetoes_on_demo(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
    second: Any,
) -> None:
    from tests.runs.test_pipeline import _refusal

    _patch(monkeypatch, second=second or _refusal(), verdict=_verdict("proceed"))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP", limits=DEMO)
    assert not result.approved
    assert result.blocked_by == "consensus"


def test_demo_risk_debate_reduce_stays_half_and_compounds(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
) -> None:
    _patch(monkeypatch, second=_trade(), verdict=_verdict("reduce"), red=_red(4))
    result = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP", limits=DEMO)
    assert result.approved and result.size_factor == 0.25


@pytest.mark.parametrize("failing", ["redteam", "riskdebate"])
def test_an_unavailable_red_team_or_risk_debate_still_blocks_on_demo(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
    failing: str,
) -> None:
    boom = AllModelsFailedError("every model failed")
    kwargs: dict[str, Any] = {
        "redteam": {"second": _trade(), "red": boom},
        "riskdebate": {"second": _trade(), "verdict": boom},
    }[failing]
    _patch(monkeypatch, **kwargs)
    result = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP", limits=DEMO)
    assert not result.approved and result.blocked_by == failing


# --- the Trader prompts -------------------------------------------------------------------


def _batch_prompt(limits: DecisionLimits | None) -> str:
    from argus.llm.agents import trader_batch
    from argus.llm.golden import CASES

    dossiers = [CASES[0].dossier()]
    if limits is None:
        return trader_batch.build_prompt(dossiers, PORTFOLIO)
    return trader_batch.build_prompt(dossiers, PORTFOLIO, limits=limits)


def _single_prompt(limits: DecisionLimits | None) -> str:
    from argus.llm.agents import trader
    from argus.llm.golden import CASES

    if limits is None:
        return trader.build_prompt(CASES[0].dossier())
    return trader.build_prompt(CASES[0].dossier(), limits=limits)


LIVE_TEXT = (
    "**at least** 1.5x the entry-to-stop distance (1.5R)",
    "the blended average of your targets at least 2R.",
    "closer than 1.5R, skip it",
    "If R1 < 1.5, move target 1 further out.",
    "If it is under 2.0, move",
)
DEMO_TEXT = (
    "**at least** 0.5x the entry-to-stop distance (0.5R)",
    "the blended average of your targets at least 0.8R.",
    "closer than 0.5R, skip it",
    "If R1 < 0.5, move target 1 further out.",
    "If it is under 0.8, move",
)


@pytest.mark.parametrize("build", [_batch_prompt, _single_prompt], ids=["batch", "single"])
def test_the_trader_prompts_name_the_active_profiles_rr(build: Any) -> None:
    for text in (build(None), build(LIVE)):
        for line in LIVE_TEXT:
            assert line in text
    demo = build(DEMO)
    for line in DEMO_TEXT:
        assert line in demo
    assert "1.5R" not in demo and "2R." not in demo
    # The ATR band is fixed on both accounts.
    for text in (build(LIVE), demo):
        assert "Under 1x or over 3.5x, fix the stop." in text


def test_the_batch_prompt_and_schema_allow_three_plans_on_demo_only() -> None:
    from argus.llm.agents import trader_batch
    from argus.llm.schemas import MAX_PLANS_PER_RUN, TraderBatch

    assert "at most 2 plans" in _batch_prompt(None) and "the two best" in _batch_prompt(None)
    assert "at most 3 plans" in _batch_prompt(DEMO) and "the three best" in _batch_prompt(DEMO)
    assert trader_batch.batch_schema(LIVE) is TraderBatch
    assert MAX_PLANS_PER_RUN == LIVE.max_entries_per_run
    plan = _trade().plan
    assert plan is not None
    three = {"plans": [plan.model_dump(mode="json") | {"symbol": s} for s in ("BTC", "ETH", "SOL")]}
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TraderBatch.model_validate(three)
    assert len(trader_batch.batch_schema(DEMO).model_validate(three).plans) == 3


# --- Q6: the extra manager slots are demo-only --------------------------------------------


def _at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 27, hh, mm, ss, tzinfo=UTC)


@pytest.mark.parametrize(
    "moment",
    [_at(0, 30), _at(4, 30), _at(9, 30), _at(12, 30), _at(16, 31), _at(20, 39, 59), _at(14, 0)],
    ids=["00:30", "04:30", "09:30", "12:30", "16:30", "20:30 + 9:59", "a far-off catch-up"],
)
@pytest.mark.parametrize("venue", ["live", "demo"])
def test_both_accounts_run_every_four_hour_slot(venue: str, moment: datetime) -> None:
    """owner 2026-10-27: "keep 4h AI runs going forward for both demo and live". The live account
    no longer refuses the 04:30/09:30/16:30/20:30 slots."""
    from argus.runs.schedule import scheduled_run_refusal

    assert scheduled_run_refusal(venue, moment) is None


def test_the_manager_timer_fires_six_times_and_the_service_says_it_is_scheduled() -> None:
    from tests.deploy.systemd_units import unit_values

    (spec,) = unit_values("argus-manager.timer", "OnCalendar")
    assert spec == "*-*-* 00,04,09,12,16,20:30:00 UTC"
    (start,) = unit_values("argus-manager.service", "ExecStart")
    assert start.endswith("argus manager --llm --scheduled")


# --- the manager threads the profile end to end (no fixture: the test account is demo) ---------


def test_a_demo_manager_run_approves_three_and_refuses_the_fourth(engine: Engine) -> None:
    from argus.features.universe import Asset
    from argus.runs.manager import run_manager
    from tests.runs.test_manager_book import _first_reject, _verdicts
    from tests.runs.test_manager_shadow import NOW as RUN_NOW
    from tests.runs.test_manager_shadow import _deps

    def four() -> list[Asset]:
        return [
            Asset("BTC", "BTC-USDT-SWAP", "bitcoin", "core"),
            Asset("ETH", "ETH-USDT-SWAP", "ethereum", "core"),
            Asset("SOL", "SOL-USDT-SWAP", "solana", "core"),
            Asset("ADA", "ADA-USDT-SWAP", "cardano", "core"),
        ]

    run_manager(engine, _deps(assets=four), now=RUN_NOW, shadow=True)

    verdicts = _verdicts(engine)
    placeable = [s for s, v in verdicts.items() if v["verdict"] in {"APPROVED", "ADJUSTED"}]
    assert len(placeable) == 3, verdicts
    fourth = next(v for s, v in verdicts.items() if s not in placeable)
    assert _first_reject(fourth["reasons"])["gate"] == 9
    assert "entries this run (max 3)" in _first_reject(fourth["reasons"])["detail"]


def test_a_live_manager_run_applies_the_live_limits_end_to_end(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real venue path, no fixture: OKX_DEMO=false with the live interlocks set. A +3 plan is
    refused at gate 1 and the third good plan at gate 9 (max 2) — both pass on demo."""
    from argus.config import get_settings
    from argus.features.universe import Asset
    from argus.runs.manager import run_manager
    from tests.runs.test_manager_book import _first_reject, _verdicts
    from tests.runs.test_manager_shadow import NOW as RUN_NOW
    from tests.runs.test_manager_shadow import _deps, _good_plan

    for key, value in {
        "OKX_DEMO": "false",
        "MODE": "auto",
        "LIVE_TRADING": "I_UNDERSTAND",
        "LIVE_VENUE": "I_UNDERSTAND_REAL_MONEY",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    assert get_settings().venue == "live" and get_settings().limits is LIVE

    def four() -> list[Asset]:
        return [Asset(s, f"{s}-USDT-SWAP", s.lower(), "core") for s in ("BTC", "ETH", "SOL", "ADA")]

    def propose(_run_id: str, dossier: Any, _market: Any) -> Any:
        plan = _good_plan(dossier.symbol)
        if dossier.symbol == "SOL":  # +3: clears demo's 2, not live's 4
            return dataclasses.replace(plan, scores=_scores(fundamental=0, onchain=0))
        return plan

    run_manager(engine, _deps(proposer=propose, assets=four), now=RUN_NOW, shadow=True)

    verdicts = _verdicts(engine)
    placeable = sorted(s for s, v in verdicts.items() if v["verdict"] in {"APPROVED", "ADJUSTED"})
    assert placeable == ["BTC", "ETH"], verdicts
    assert _first_reject(verdicts["SOL"]["reasons"])["gate"] == 1
    third = _first_reject(verdicts["ADA"]["reasons"])
    assert third["gate"] == 9 and "entries this run (max 2)" in third["detail"]


@pytest.mark.parametrize("build", [_batch_prompt, _single_prompt], ids=["batch", "single"])
def test_the_no_trade_stance_explores_on_demo_and_is_unchanged_on_live(build: Any) -> None:
    """2026-10-26 extension of owner's grilling decisions: with the demo numbers relaxed, the Trader
    still returned no plans, because the prompt told it an empty answer is "a good outcome". On
    demo it is told to return its best candidates and express doubt in the scores; live keeps the
    old paragraph word for word."""
    for text in (build(None), build(LIVE)):
        assert "That is a good outcome" in text
        assert "DEMO account" not in text
    demo = build(DEMO)
    assert "That is a good outcome" not in demo
    assert "DEMO account" in demo
    assert "withholding" in demo
    assert "Risk Gate's job" in demo


def test_a_one_sided_consensus_halves_on_demo_and_vetoes_on_live(
    monkeypatch: pytest.MonkeyPatch,
    dossier,  # noqa: ANN001
) -> None:
    """owner 2026-10-26: the first demo plans to reach consensus (ETH, BNB, ARB) were all vetoed
    because the second opinion declined. On demo a declining second opinion halves the size
    (like a risk-debate "decline"); a real disagreement on side or stop still vetoes."""
    from tests.runs.test_pipeline import _refusal

    _patch(monkeypatch, second=_refusal(), verdict=_verdict("proceed"), red=_red(2))
    demo = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP", limits=DEMO)
    assert demo.approved, demo.checks
    assert demo.size_factor == 0.5
    assert any(a == "consensus" and "one_sided" in o for a, o in demo.checks)

    _patch(monkeypatch, second=_refusal(), verdict=_verdict("proceed"), red=_red(2))
    live = vet(FakeRouter(), "r1", dossier, _trade(), "g", PORTFOLIO, "TREND_UP")
    assert not live.approved and live.blocked_by == "consensus"


def test_a_demo_run_repairs_a_too_close_target_and_journals_the_repaired_plan(
    engine: Engine,
) -> None:
    """owner 2026-10-26: ENA passed every agent and was rejected at TP1 0.15R. On demo the manager
    moves the targets out to the floor before the gate; the journalled plan carries them."""
    import json

    from sqlalchemy import text

    from argus.features.universe import Asset
    from argus.runs.manager import run_manager
    from tests.runs.test_manager_book import _verdicts
    from tests.runs.test_manager_shadow import NOW as RUN_NOW
    from tests.runs.test_manager_shadow import _deps, _good_plan

    def one() -> list[Asset]:
        return [Asset("BTC", "BTC-USDT-SWAP", "bitcoin", "core")]

    def close_target(_run_id: str, dossier: Any, _market: Any) -> Any:
        plan = _good_plan(dossier.symbol)  # entry 100, stop 94: R = 6
        return dataclasses.replace(plan, targets=[Target(100.9, 50), Target(102.0, 50)])

    run_manager(engine, _deps(proposer=close_target, assets=one), now=RUN_NOW, shadow=True)

    verdict = _verdicts(engine)["BTC"]
    assert verdict["verdict"] in {"APPROVED", "ADJUSTED"}, verdict
    with engine.connect() as conn:
        payload = conn.execute(text("SELECT payload FROM plans")).scalar_one()
    tp1 = json.loads(payload)["targets"][0][0]
    assert (tp1 - 100.0) / 6.0 >= DEMO.min_rr_tp1


@pytest.mark.parametrize("build", [_batch_prompt, _single_prompt], ids=["batch", "single"])
def test_the_demo_trader_is_told_code_repairs_target_distance(build: Any) -> None:
    """owner 2026-10-27: overnight the Trader still dropped every setup as "not meeting 0.8R",
    although code now moves a too-close target out to the floor (repair, demo only)."""
    assert "R:R floor, so do not" in build(DEMO)
    for text in (build(None), build(LIVE)):
        assert "R:R floor, so do not" not in text
