"""Asset memory cards (ARCHITECTURE.md §11): deterministic, rebuilt each run, persisted."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from argus.features import asset_memory as am
from argus.journal import ledger
from argus.journal.db import open_journal, start_run

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def _daily(n: int = 120, spread: float = 2.0) -> pd.DataFrame:
    c = pd.Series(100.0 + np.zeros(n))
    return pd.DataFrame(
        {"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 1.0}
    )


def test_typical_atr_is_the_median_daily_atr_percent() -> None:
    card = am.build_card(
        "SOL",
        daily=_daily(spread=2.0),
        funding_rates=[],
        catalysts=[],
        tokenomics={},
        theses=[],
        today=NOW.date(),
    )
    assert card["atr_pct_med_90d"] == pytest.approx(4.0, rel=1e-3)


def test_funding_regime_summarises_history_and_places_the_latest() -> None:
    rates = [0.0001] * 20 + [0.0003]
    card = am.build_card(
        "SOL",
        daily=_daily(),
        funding_rates=rates,
        catalysts=[],
        tokenomics={},
        theses=[],
        today=NOW.date(),
    )
    assert card["funding_med_30d%"] == pytest.approx(0.01)
    assert card["funding_now_pctile"] == pytest.approx(100.0)


def test_unlock_cadence_comes_from_the_catalyst_calendar() -> None:
    catalysts = [
        {"symbol": "ARB", "kind": "unlock", "date": date(2026, 9, 16)},
        {"symbol": "ARB", "kind": "unlock", "date": date(2026, 10, 16)},
        {"symbol": "ARB", "kind": "listing", "date": date(2026, 9, 30)},
        {"symbol": "SOL", "kind": "unlock", "date": date(2026, 9, 20)},
    ]
    card = am.build_card(
        "ARB",
        daily=_daily(),
        funding_rates=[],
        catalysts=catalysts,
        tokenomics={},
        theses=[],
        today=NOW.date(),
    )
    assert card["next_unlock"] == "2026-10-16"
    assert card["unlocks_last_90d"] == 1  # 16 Sep happened; cadence counts history


def test_tokenomics_and_past_theses_are_carried() -> None:
    theses = [{"side": "short", "state": "FILLED", "created_at": "2026-09-01", "result_r": 2.1}]
    card = am.build_card(
        "ARB",
        daily=_daily(),
        funding_rates=[],
        catalysts=[],
        tokenomics={"token_revenue_share": 0.0, "note": "DAO keeps revenue"},
        theses=theses,
        today=NOW.date(),
    )
    assert card["rev_to_token"] == 0.0
    assert card["theses"] == theses


def test_thin_history_leaves_fields_out_instead_of_inventing_them() -> None:
    card = am.build_card(
        "NEW",
        daily=_daily(n=5),
        funding_rates=[],
        catalysts=[],
        tokenomics={},
        theses=[],
        today=NOW.date(),
    )
    assert "atr_pct_med_90d" not in card
    assert "funding_med_30d%" not in card


def test_cards_persist_and_past_theses_are_read_from_the_plan_ledger(tmp_path: Path) -> None:
    engine = open_journal(tmp_path / "j.db")
    with start_run(engine, "manager", "cfg") as run_id:
        pass
    ledger.append(
        engine,
        "plans",
        {
            "plan_id": "p1",
            "run_id": run_id,
            "mode": "demo",
            "symbol": "SOL",
            "side": "long",
            "state": "REJECTED",
            "payload": "{}",
        },
    )
    theses = am.recent_theses(engine, "SOL")
    assert theses[0]["side"] == "long" and theses[0]["state"] == "REJECTED"

    am.save_card(engine, "SOL", {"atr_pct_med_90d": 4.1}, NOW)
    am.save_card(engine, "SOL", {"atr_pct_med_90d": 4.3}, NOW + timedelta(hours=12))
    assert am.load_card(engine, "SOL") == {"atr_pct_med_90d": 4.3}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM asset_memory")).scalar() == 1
