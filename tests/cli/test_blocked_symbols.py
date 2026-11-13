"""A blocked symbol is never a new entry, and never loses its metadata while held (D-15).

`_allowed` dropped blocked symbols from core and whitelist, but `argus manager --symbols TRUMP`
then missed the lookup and built a bare satellite Asset that was researched, proposed and
placeable. And review looked held positions up in the same filtered list, so a position on a
newly blocked symbol lost its coingecko, defillama and unlock families.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import argus.cli as cli
from argus.features import universe as uni


@pytest.fixture
def knowledge(monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(UTC).date()
    files: dict[str, Any] = {
        "whitelist.yaml": {
            "entries": [
                {
                    "symbol": "TRUMP",
                    "okx": "TRUMP-USDT-SWAP",
                    "coingecko": "trump",
                    "renewed": today,
                }
            ]
        },
        "blocklist.yaml": {"symbols": ["TRUMP", "USELESS"]},
    }
    monkeypatch.setattr(uni, "load_yaml", lambda name, *a, **k: files[name])
    core = [
        {"symbol": "BTC", "okx": "BTC-USDT-SWAP", "coingecko": "bitcoin"},
        {
            "symbol": "USELESS",
            "okx": "USELESS-USDT-SWAP",
            "coingecko": "useless",
            "defillama": {"protocol": "useless"},
            "unlocks_slug": "useless",
        },
    ]
    monkeypatch.setattr(uni, "universe_config", lambda: {"core": core})


def _run_manager(monkeypatch: pytest.MonkeyPatch, symbols: str) -> tuple[Any, dict[str, bool]]:
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(mode="shadow", venue="demo"))
    reached: dict[str, bool] = {}

    def stop_here(*_a: object, **_k: object) -> None:
        reached["journal"] = True
        raise SystemExit(0)

    monkeypatch.setattr("argus.journal.db.open_journal", stop_here)
    result = CliRunner().invoke(cli.app, ["manager", "--shadow", "--symbols", symbols])
    return result, reached


def test_manager_refuses_a_blocklisted_symbol_passed_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, knowledge: None
) -> None:
    result, reached = _run_manager(monkeypatch, "TRUMP")
    assert result.exit_code == 2, result.output
    assert "TRUMP" in result.output
    assert not reached, "a blocklisted symbol reached the run"


def test_manager_refuses_an_excluded_kind_passed_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch, knowledge: None
) -> None:
    result, reached = _run_manager(monkeypatch, "BTC3L")
    assert result.exit_code == 2, result.output
    assert not reached


def test_manager_keeps_the_allowed_symbols_and_says_which_it_dropped(
    monkeypatch: pytest.MonkeyPatch, knowledge: None
) -> None:
    result, reached = _run_manager(monkeypatch, "trump,BTC")
    assert reached.get("journal"), result.output
    assert "TRUMP" in result.output


def test_review_keeps_the_metadata_of_a_held_symbol_that_is_now_blocked(knowledge: None) -> None:
    known = cli._known_assets()
    held = known["USELESS"]
    assert held.coingecko == "useless"
    assert held.defillama == {"protocol": "useless"} and held.unlocks_slug == "useless"
    assert known["TRUMP"].coingecko == "trump"
    # New entries still come from the filtered lists only.
    assert [a.symbol for a in cli._listed_assets()] == ["BTC"]
