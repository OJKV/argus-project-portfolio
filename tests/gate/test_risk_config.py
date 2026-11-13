"""`config/risk_profiles.yaml` is validated, and what it says is what runs (review G-N5/G-17/G-5).

Before: the YAML was a bare dict and only `small_account_steps` was read. `risk_pct: 35` loaded,
`small_account_steps: []` meant every step, typos vanished, `max_positions: 2` and the loss limits
were decoration, and the profile was aggressive at any equity.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text

import argus.runs.manager as manager_mod
from argus.config import get_settings, load_yaml
from argus.gate import risk_config
from argus.gate.rules import OpenPosition
from argus.journal.db import new_id, open_journal
from argus.runs.manager import run_manager
from tests.runs.test_manager_shadow import NOW, _deps

RAW = load_yaml("risk_profiles.yaml")


def _raw(**edits: Any) -> dict[str, Any]:
    out = copy.deepcopy(RAW)
    for path, value in edits.items():
        node = out
        *parents, leaf = path.split("__")
        for key in parents:
            node = node[int(key) if key.isdigit() else key]
        node[int(leaf) if leaf.isdigit() else leaf] = value
    return out


def test_the_shipped_file_is_valid() -> None:
    config = risk_config.parse(RAW)
    assert config.aggressive.max_positions == 2
    assert config.conservative.small_account_steps == [0, 1, 2]


@pytest.mark.parametrize(
    "bad",
    [
        _raw(small_account_steps__1__risk_pct=35),  # the typo the loader used to accept
        _raw(aggressive__small_account_steps=[]),  # used to mean "all steps"
        _raw(conservative__small_account_steps=[0, 7]),  # a step that does not exist
        _raw(small_account_steps__3__max_lev=50),  # above common.max_leverage
        _raw(small_account_steps__0__min_lev=20),  # min above max
    ],
)
def test_impossible_values_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        risk_config.parse(bad)


def test_a_misspelt_key_is_refused_not_ignored() -> None:
    bad = copy.deepcopy(RAW)
    bad["aggressive"]["max_postions"] = 1
    with pytest.raises(ValidationError, match="max_postions"):
        risk_config.parse(bad)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("common__daily_loss_halt_pct", 2.0),
        ("common__min_rr_tp1", 1.2),
        ("common__stop_atr_band", [1.0, 5.0]),
        ("min_viable_notional_usd", 10),
        ("regime_multipliers__CHOP", 1.0),
    ],
)
def test_editing_a_pinned_number_in_the_file_is_refused(key: str, value: Any) -> None:
    """The gate enforces these in code. A file that disagrees would change nothing, silently."""
    with pytest.raises(ValidationError, match="disagrees"):
        risk_config.parse(_raw(**{key: value}))


def test_the_profile_follows_equity_with_a_seven_day_exit() -> None:
    config = risk_config.parse(RAW)
    assert risk_config.select_profile(config, 150.0) == "aggressive"
    assert risk_config.select_profile(config, 2000.0, lowest_recent_equity=None) == "aggressive"
    assert risk_config.select_profile(config, 2000.0, lowest_recent_equity=450.0) == "aggressive"
    assert risk_config.select_profile(config, 2000.0, lowest_recent_equity=520.0) == "conservative"
    assert risk_config.select_profile(config, 150.0, override="conservative") == "conservative"


def test_step_3_only_below_the_small_account_threshold() -> None:
    config = risk_config.parse(RAW)
    small = risk_config.step_policies(config, "aggressive", 150.0)
    large = risk_config.step_policies(config, "aggressive", 800.0)
    assert [s.step for s in small] == [0, 1, 2, 3]
    assert [s.step for s in large] == [0, 1, 2]


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.mark.usefixtures("live_limits")
def test_a_small_account_holds_at_most_two_positions(engine: Engine) -> None:
    """§5.1: "<= 2 open positions while equity < $500 — never relaxes". The gate's constant said 4
    and `aggressive.max_positions: 2` was never read."""
    held = [
        OpenPosition("ARB", "short", 20.0, 0.5, 1.1, 900.0),
        OpenPosition("OP", "short", 20.0, 0.5, 1.1, 900.0),
    ]
    run_manager(engine, _deps(portfolio=lambda: (150.0, 150.0, held)), now=NOW, shadow=True)
    with engine.connect() as conn:
        reasons = [r[0] for r in conn.execute(text("SELECT reasons FROM gate_results"))]
    assert reasons and all('"gate": 6' in r and "open positions (max 2)" in r for r in reasons)


def test_a_forced_conservative_profile_still_holds_two_under_500(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.1's "<= 2 open positions while equity < $500" never relaxes, and `/risk conservative`
    is the documented operator action under $500. Forcing it handed the gate conservative's 4."""
    config = risk_config.parse(RAW)
    assert risk_config.max_positions(config, "conservative", 150.0) == 2
    assert risk_config.max_positions(config, "conservative", 800.0) == 4

    monkeypatch.setenv("RISK_PROFILE", "conservative")
    get_settings.cache_clear()
    profile, steps, cap = manager_mod.risk_policy(engine, 150.0, NOW)
    assert profile == "conservative" and [s.step for s in steps] == [0, 1, 2]
    assert cap == 2


def test_a_broken_risk_file_fails_the_run(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manager_mod, "risk_config", lambda: _raw(small_account_steps__1__risk_pct=35)
    )
    result = run_manager(engine, _deps(), now=NOW, shadow=True)
    assert result.status == "FAILED" and result.plans == 0


def test_the_seven_day_exit_reads_the_equity_history(engine: Engine) -> None:
    from argus.runs.manager import lowest_equity_since

    with engine.begin() as conn:
        run_id = new_id()
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r, 'watchdog', 'demo', :t, 'OK', 0, 'demo')"
            ),
            {"r": run_id, "t": NOW.isoformat()},
        )
        for days, equity in ((9, "540"), (5, "610"), (1, "700")):
            conn.execute(
                text(
                    "INSERT INTO equity_snapshots (snapshot_id, run_id, created_at, equity)"
                    " VALUES (:i, :r, :t, :e)"
                ),
                {
                    "i": new_id(),
                    "r": run_id,
                    "t": (NOW - timedelta(days=days)).isoformat(),
                    "e": equity,
                },
            )
    assert lowest_equity_since(engine, NOW - timedelta(days=7), NOW, 650.0) == 610.0
    # A window the journal does not reach back to cannot be vouched for.
    assert lowest_equity_since(engine, NOW - timedelta(days=30), NOW, 650.0) is None
