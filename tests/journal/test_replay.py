"""Replay reads back a run end to end — the payload writer and reader must not drift apart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from argus.journal.db import open_journal
from argus.journal.replay import RunNotFoundError, as_dict, load
from argus.runs.manager import run_manager
from tests.runs.test_manager_shadow import NOW, _deps, _weak_plan


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


def _run(engine: Engine, proposer: Any = None) -> str:
    return run_manager(engine, _deps(proposer), now=NOW, shadow=True).run_id


def test_missing_run_is_an_error_not_an_empty_report(engine: Engine) -> None:
    with pytest.raises(RunNotFoundError):
        load(engine, "does-not-exist")


def test_replay_reconstructs_the_run_without_touching_the_network(engine: Engine) -> None:
    run_id = _run(engine)
    replayed = load(engine, run_id)

    assert replayed.run["kind"] == "manager"
    assert replayed.run["status"] == "OK"
    assert replayed.regime is not None
    assert replayed.regime["label"] == "TREND_UP"
    assert [d["symbol"] for d in replayed.dossiers] == ["BTC", "SOL"]
    assert len(replayed.decisions) == 2
    assert sum(replayed.counts.values()) == 2


def test_a_rejection_explains_itself_from_stored_rows(engine: Engine) -> None:
    run_id = _run(engine, lambda run_id, dossier, market: _weak_plan(dossier.symbol))
    decision = load(engine, run_id).decisions[0]

    assert decision.verdict == "REJECTED"
    assert decision.rejections, "a REJECTED verdict must carry at least one REJECT reason"
    assert all("name" in r and "detail" in r for r in decision.rejections)


def test_the_gate_inputs_are_journaled_with_the_plan(engine: Engine) -> None:
    """Without the market snapshot a replay can show the verdict but never explain it."""
    market = load(engine, _run(engine)).decisions[0].market

    assert market["mark"] == 100.0
    assert market["regime"] == "TREND_UP"
    assert market["taker_fee"] == "-0.0005"
    assert market["max_leverage"] == 50


def test_json_dump_is_serialisable(engine: Engine) -> None:
    import json

    payload = as_dict(load(engine, _run(engine)))
    assert json.loads(json.dumps(payload, default=str))["counts"]


def test_replay_summarises_the_llm_calls_behind_a_run(engine: Engine) -> None:
    """Without this, "1 plan from 15 assets" is ambiguous: 14 considered refusals and 14 silent
    failures look identical from the gate's side, and they demand opposite responses."""
    from sqlalchemy import text as sql

    run_id = _run(engine)
    with engine.begin() as conn:
        for status, provider, model in (
            ("ok", "gemini", "x"),
            ("ok", "gemini", "x"),
            ("error", "groq", "y"),
        ):
            conn.execute(
                sql(
                    "INSERT INTO llm_calls (call_id, run_id, created_at, agent, tier, provider,"
                    " model, family, attempt, status) VALUES (lower(hex(randomblob(16))), :r,"
                    " '2026-09-20T00:00:00+00:00', 'trader', 'strong_json', :p, :m, 'f', 1, :s)"
                ),
                {"r": run_id, "p": provider, "m": model, "s": status},
            )

    replayed = load(engine, run_id)
    assert replayed.llm_calls["ok"] == 2
    assert replayed.llm_calls["error"] == 1
    assert replayed.llm_models["gemini/x"] == 2
