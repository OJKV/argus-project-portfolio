"""`argus halt | resume | flatten` — the operator's control plane (review 2026-09-18 P-2, still
OPEN on 2026-10-07).

Every incident procedure in HUMAN_GUIDE and RUNBOOK began with `/halt` or `/close all`, and neither
existed: the Telegram router has no caller. The only halt was SSH + creating the file by hand, and
the only flatten was the OKX web UI. These run on FakeExchange; nothing here reaches a venue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import argus.cli as cli
from argus import config
from argus.exchange.fake import FakeExchange
from argus.journal.db import open_journal
from argus.notify import telegram
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.lock import run_lock
from argus.runs.watchdog import KILL_MARKER, read_halt, write_halt

BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"


@pytest.fixture
def halt_file() -> Path:
    config.get_settings.cache_clear()
    return config.get_settings().halt_file


@pytest.fixture
def venue(monkeypatch: pytest.MonkeyPatch) -> FakeExchange:
    exchange = FakeExchange()
    exchange.open_unprotected(BTC)
    exchange.open_unprotected(ETH, side="short")
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", lambda *a, **k: exchange)
    return exchange


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> RecordingBot:
    recorder = RecordingBot()
    monkeypatch.setattr(
        telegram.Notifier, "from_settings", classmethod(lambda cls, s=None: Notifier(recorder, "1"))
    )
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    return recorder


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, list(args))
    return result.exit_code, result.output


# ── halt ──────────────────────────────────────────────────────────────────────────────────────


def test_halt_writes_the_file_every_run_checks(halt_file: Path) -> None:
    code, out = _run("halt", "news spike, standing aside")
    assert code == 0, out
    assert "news spike, standing aside" in (read_halt(halt_file) or "")


def test_halt_never_overwrites_a_kill_switch_halt(halt_file: Path) -> None:
    """The kill marker is what makes the watchdog retry an unfinished flatten (X-4)."""
    write_halt(halt_file, f"{KILL_MARKER} drawdown 16.0% from 30d high")
    code, out = _run("halt", "me too")
    assert code == 0, out
    assert KILL_MARKER in (read_halt(halt_file) or "")


# ── resume ────────────────────────────────────────────────────────────────────────────────────


def test_resume_clears_an_operator_halt(halt_file: Path) -> None:
    _run("halt", "lunch")
    code, out = _run("resume")
    assert code == 0, out
    assert read_halt(halt_file) is None


def test_resume_after_the_kill_switch_needs_the_explicit_flag(halt_file: Path) -> None:
    write_halt(halt_file, f"{KILL_MARKER} drawdown 16.0% from 30d high")
    code, out = _run("resume")
    assert code == 2
    assert read_halt(halt_file) is not None
    assert "--after-kill-switch" in out

    code, out = _run("resume", "--after-kill-switch")
    assert code == 0, out
    assert read_halt(halt_file) is None


def test_resume_when_not_halted_is_a_no_op(halt_file: Path) -> None:
    code, out = _run("resume")
    assert code == 0 and "not halted" in out


# ── flatten ───────────────────────────────────────────────────────────────────────────────────


def test_flatten_without_yes_changes_nothing(
    halt_file: Path, venue: FakeExchange, bot: RecordingBot
) -> None:
    code, out = _run("flatten")
    assert code == 2
    assert set(venue.positions) == {BTC, ETH}
    assert read_halt(halt_file) is None


def test_flatten_halts_then_closes_everything(
    halt_file: Path, venue: FakeExchange, bot: RecordingBot
) -> None:
    code, out = _run("flatten", "--yes")
    assert code == 0, out
    assert venue.positions == {}
    # Written with the kill marker, so a close that fails now is retried by every watchdog tick.
    assert KILL_MARKER in (read_halt(halt_file) or "")
    assert any("flatten" in text for _, text in bot.sent)


def test_flatten_cancels_resting_entries_too(
    halt_file: Path, venue: FakeExchange, bot: RecordingBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled: list[str] = []
    monkeypatch.setattr(venue, "cancel_all", lambda symbol: cancelled.append(symbol))
    assert _run("flatten", "--yes")[0] == 0
    assert set(cancelled) == {BTC, ETH}


def test_flatten_one_symbol_leaves_the_rest_and_does_not_halt(
    halt_file: Path, venue: FakeExchange, bot: RecordingBot
) -> None:
    code, out = _run("flatten", "--symbol", BTC, "--yes")
    assert code == 0, out
    assert set(venue.positions) == {ETH}
    assert read_halt(halt_file) is None


def test_a_close_that_fails_exits_non_zero_and_stays_halted(
    halt_file: Path, venue: FakeExchange, bot: RecordingBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(symbol: str) -> str:
        raise RuntimeError("51000 venue says no")

    monkeypatch.setattr(venue, "close", refuse)
    code, out = _run("flatten", "--yes")
    assert code == 1
    assert KILL_MARKER in (read_halt(halt_file) or "")


def test_flatten_waits_for_a_running_trade_rather_than_racing_it(
    halt_file: Path, venue: FakeExchange, bot: RecordingBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager mid-placement only checks HALT at its start; closing under it is how a fresh
    entry slips past the flatten. The HALT goes down first either way."""
    monkeypatch.setattr(cli, "FLATTEN_LOCK_WAIT_S", 0.2)
    with run_lock("manager", config.get_settings().argus_home):
        code, out = _run("flatten", "--yes")
    assert code == 1
    assert set(venue.positions) == {BTC, ETH}
    assert KILL_MARKER in (read_halt(halt_file) or "")
    assert "watchdog" in out


def test_flatten_is_journalled(halt_file: Path, venue: FakeExchange, bot: RecordingBot) -> None:
    from sqlalchemy import text

    _run("flatten", "--yes")
    engine = open_journal()
    with engine.connect() as conn:
        kinds = [r[0] for r in conn.execute(text("SELECT kind FROM runs"))]
    assert "flatten" in kinds


# ── flatten against the venue's own symbol spelling ─────────────────────────────────────────


class UnifiedVenue:
    """The shape `OKXAdapter.reconcile()` really returns.

    `position_row` keeps ccxt's unified symbol (`BTC/USDT:USDT`), while the operator, `--help` and
    the RUNBOOK type the OKX id (`BTC-USDT-SWAP`). FakeExchange keys positions by the OKX id, which
    is how `flatten --symbol` closing nothing and exiting 0 passed its tests (review 2026-10-07,
    second pass).
    """

    def __init__(self) -> None:
        self.positions = {"BTC/USDT:USDT": "long", "ETH/USDT:USDT": "short"}
        self.orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def reconcile(self) -> dict[str, Any]:
        return {
            "positions": [{"symbol": s, "side": side} for s, side in self.positions.items()],
            "open_orders": list(self.orders),
            "unprotected": [],
        }

    def fetch_open_orders(self) -> list[dict[str, Any]]:
        return list(self.orders)

    def cancel_all(self, symbol: str) -> None:
        self.cancelled.append(symbol)
        self.orders = [o for o in self.orders if o["symbol"] != symbol]

    def close(self, symbol: str) -> str:
        self.positions.pop(symbol)
        return "o1"


@pytest.fixture
def unified(monkeypatch: pytest.MonkeyPatch) -> UnifiedVenue:
    venue = UnifiedVenue()
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", lambda *a, **k: venue)
    return venue


def test_flatten_one_symbol_matches_the_venues_unified_spelling(
    halt_file: Path, unified: UnifiedVenue, bot: RecordingBot
) -> None:
    code, out = _run("flatten", "--symbol", BTC, "--yes")
    assert code == 0, out
    assert set(unified.positions) == {"ETH/USDT:USDT"}, "the BTC position is still open"
    assert "BTC/USDT:USDT" in out


def test_flatten_of_a_symbol_with_no_position_fails_loudly(
    halt_file: Path, unified: UnifiedVenue, bot: RecordingBot
) -> None:
    """The incident row for a position with no stop ends in this command; reporting success over
    "closed nothing" leaves the position open with the operator believing it shut."""
    code, out = _run("flatten", "--symbol", "SOL-USDT-SWAP", "--yes")
    assert code != 0
    assert "no open position" in out
    assert set(unified.positions) == {"BTC/USDT:USDT", "ETH/USDT:USDT"}


# ── flatten cancels resting entries on symbols with no position (X-N4) ─────────────────────


def test_flatten_cancels_a_resting_entry_on_a_flat_symbol(
    halt_file: Path, unified: UnifiedVenue, bot: RecordingBot
) -> None:
    """The usual state right after a manager run: an unfilled limit entry on a symbol we hold
    nothing in. Cancelling only where a position exists left it resting, to fill after the
    emergency flatten and be closed again by the kill-switch at the cost of fees and slippage."""
    unified.orders = [{"id": "e1", "symbol": "SOL/USDT:USDT"}]
    code, out = _run("flatten", "--yes")
    assert code == 0, out
    assert "SOL/USDT:USDT" in unified.cancelled
    assert unified.positions == {}


def test_flatten_one_symbol_cancels_only_that_symbols_resting_entries(
    halt_file: Path, unified: UnifiedVenue, bot: RecordingBot
) -> None:
    unified.orders = [
        {"id": "e1", "symbol": "SOL/USDT:USDT"},
        {"id": "e2", "symbol": "BTC/USDT:USDT"},
    ]
    code, out = _run("flatten", "--symbol", BTC, "--yes")
    assert code == 0, out
    assert unified.cancelled == ["BTC/USDT:USDT"]


def test_flatten_one_flat_symbol_with_a_resting_entry_cancels_it(
    halt_file: Path, unified: UnifiedVenue, bot: RecordingBot
) -> None:
    unified.orders = [{"id": "e1", "symbol": "SOL/USDT:USDT"}]
    code, out = _run("flatten", "--symbol", "SOL-USDT-SWAP", "--yes")
    assert code == 0, out
    assert unified.cancelled == ["SOL/USDT:USDT"]
    assert set(unified.positions) == {"BTC/USDT:USDT", "ETH/USDT:USDT"}
