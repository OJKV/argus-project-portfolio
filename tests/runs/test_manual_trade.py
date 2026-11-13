"""`argus manual-trade` — one deliberate DEMO trade, by the production path (CLAUDE.md #1).

owner may ask, in his own words and in the session, for a single manual trade. This is the
mechanism,
and these tests are what keep it from becoming anything wider: it refuses the live account, refuses
when not armed, places nothing without `--yes`, sizes at the venue's own minimum, goes through
`place_bracket` so the stop travels with the entry (#4), and leaves the journal in the state the
watchdog, reconcile and review need to treat the position as OURS rather than an orphan.

Everything here runs on FakeExchange. Nothing reaches a venue.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from typer.testing import CliRunner

import argus.cli as cli
from argus import config
from argus.config import Settings
from argus.exchange.fake import DEFAULT_SPECS, FakeExchange
from argus.exchange.okx import build_bracket_params
from argus.execution.idempotency import client_order_id
from argus.execution.lifecycle import order_state
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.lock import run_lock
from argus.runs.watchdog import Watchdog, plan_stops_from_journal

# The spelling the adapter actually accepts: ccxt keys its markets by unified symbol, and the demo
# tests that prove placement end to end pass this one. `BTC-USDT-SWAP` is an instId, not a market.
BTC = "BTC/USDT:USDT"
ARGS = ["manual-trade", "--symbol", "BTC", "--side", "long", "--stop-pct", "2.5"]
ARGS += ["--target-pct", "5.0"]
MIN_SIZE = "0.05"  # deliberately not the fake's default 0.01: the size must come from the venue


class Venue:
    """The fake the CLI is handed, and a record of whether anything asked for an adapter."""

    def __init__(self, fake: FakeExchange) -> None:
        self.fake = fake
        self.built = 0


@pytest.fixture
def venue(monkeypatch: pytest.MonkeyPatch) -> Venue:
    spec = {**DEFAULT_SPECS["BTC-USDT-SWAP"], "min_size": Decimal(MIN_SIZE)}
    recorded = Venue(FakeExchange(marks={BTC: Decimal("75000")}, specs={BTC: spec}))

    def factory(*_a: Any, **_k: Any) -> FakeExchange:
        recorded.built += 1
        return recorded.fake

    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", factory)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    return recorded


def _arm(monkeypatch: pytest.MonkeyPatch, *, mode: str = "approve", demo: bool = True) -> None:
    monkeypatch.setenv("MODE", mode)
    if mode in ("approve", "auto"):
        monkeypatch.setenv("LIVE_TRADING", "I_UNDERSTAND")
    monkeypatch.setenv("OKX_DEMO", "true" if demo else "false")
    if not demo:
        monkeypatch.setenv("LIVE_VENUE", "I_UNDERSTAND_REAL_MONEY")
    config.get_settings.cache_clear()


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli.app, list(args))
    return result.exit_code, result.output


def _rows(sql: str, **params: Any) -> list[Any]:
    engine = open_journal()
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).mappings())


# ── the interlocks ────────────────────────────────────────────────────────────────────────────


def test_it_refuses_the_live_account(monkeypatch: pytest.MonkeyPatch, venue: Venue) -> None:
    _arm(monkeypatch, demo=False)
    code, out = _run(*ARGS, "--yes")
    assert code != 0
    assert "demo" in out.lower()
    assert venue.built == 0, "an adapter was built for the live account"
    assert venue.fake.orders == {}


def test_it_refuses_when_the_run_is_not_armed(
    monkeypatch: pytest.MonkeyPatch, venue: Venue
) -> None:
    _arm(monkeypatch, mode="demo")
    code, out = _run(*ARGS, "--yes")
    assert code == 2
    assert "MODE" in out
    assert venue.built == 0
    assert venue.fake.orders == {}


def test_without_yes_it_only_prints_the_order(armed: None, venue: Venue) -> None:
    code, out = _run(*ARGS)
    assert code == 0, out
    # The exact order, in the words the operator will check it against.
    for fragment in (
        "BTC/USDT:USDT",
        MIN_SIZE,
        "market",
        "isolated",
        "2x",
        "73125.0",  # 75000 less 2.5 %
        "78750.0",  # 75000 plus 5 %
        "--yes",
    ):
        assert fragment in out, (fragment, out)
    assert venue.fake.orders == {} and venue.fake.algos == {} and venue.fake.positions == {}
    assert _rows("SELECT * FROM orders") == []
    assert _rows("SELECT * FROM runs WHERE kind = 'manual'") == []


# ── the order ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("minimum", ["0.05", "0.37"])
def test_the_size_is_the_venues_minimum(
    armed: None, venue: Venue, monkeypatch: pytest.MonkeyPatch, minimum: str
) -> None:
    venue.fake.specs[BTC] = {**venue.fake.specs[BTC], "min_size": Decimal(minimum)}
    code, out = _run(*ARGS, "--yes")
    assert code == 0, out
    (order,) = venue.fake.orders.values()
    assert order.size == Decimal(minimum)
    assert order.order_type == "market" and order.side == "buy"


def test_leverage_is_two_and_margin_is_isolated(armed: None, venue: Venue) -> None:
    captured: list[dict[str, Any]] = []
    original = venue.fake.place_bracket

    def spy(order: dict[str, Any]) -> dict[str, str]:
        captured.append(order)
        return original(order)

    venue.fake.place_bracket = spy  # type: ignore[method-assign]
    assert _run(*ARGS, "--yes")[0] == 0
    assert venue.fake.leverage[BTC] == 2
    assert build_bracket_params(captured[0])["tdMode"] == "isolated"


def test_the_stop_is_attached_through_place_bracket(armed: None, venue: Venue) -> None:
    """Rule #4: the stop travels with the entry, in the production payload, not as a follow-up."""
    captured: list[dict[str, Any]] = []
    original = venue.fake.place_bracket

    def spy(order: dict[str, Any]) -> dict[str, str]:
        captured.append(order)
        return original(order)

    venue.fake.place_bracket = spy  # type: ignore[method-assign]
    code, out = _run(*ARGS, "--yes")
    assert code == 0, out
    (order,) = captured
    sl = [a for a in order["attach_algos"] if a["kind"] == "sl"]
    assert [Decimal(str(a["trigger_px"])) for a in sl] == [Decimal("73125.0")]
    # The unchanged production serialiser accepts it: a stop-loss is present in the OKX payload.
    params = build_bracket_params(order)
    assert any("slTriggerPx" in a for a in params["attachAlgoOrds"])
    assert [(a.kind, a.trigger_px) for a in venue.fake.algos.values() if a.kind == "sl"] == [
        ("sl", Decimal("73125.0"))
    ]


def test_a_stop_the_venue_refuses_leaves_no_position_and_says_so(
    armed: None, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production path on a refused bracket: the entry never exists, the order is REJECTED."""

    def refuse(order: dict[str, Any]) -> dict[str, str]:
        raise RuntimeError("51049 stop is on the wrong side")

    monkeypatch.setattr(venue.fake, "place_bracket", refuse)
    code, out = _run(*ARGS, "--yes")
    assert code == 1
    assert "51049" in out
    assert venue.fake.positions == {} and venue.fake.orders == {}
    (row,) = _rows("SELECT order_id FROM orders")
    assert order_state(open_journal(), row["order_id"]) == "REJECTED"


# ── the journal ───────────────────────────────────────────────────────────────────────────────


def test_it_is_journalled_like_a_manager_order_with_the_venue_tag(
    armed: None, venue: Venue
) -> None:
    code, out = _run(*ARGS, "--yes")
    assert code == 0, out

    (run,) = _rows("SELECT * FROM runs")
    assert run["kind"] == "manual" and run["status"] == "OK"
    assert run["config_hash"] == "demo", "F-131/F-149: the account the run traded"
    assert run["shadow"] == 0

    (plan,) = _rows("SELECT * FROM plans")
    assert plan["run_id"] == run["run_id"] and plan["symbol"] == "BTC" and plan["side"] == "long"
    payload = json.loads(plan["payload"])
    assert payload["stop"] == 73125.0 and payload["targets"] == [[78750.0, 100]]
    assert payload["proposer"] == "manual" and payload["entry_kind"] == "market"
    # Every key `_record_decision` writes, so the Reviewer, post-mortem and dashboard find them.
    for key in ("entry", "time_stop_hours", "scores", "thesis", "invalidation_rules", "market"):
        assert key in payload, key

    (gate,) = _rows("SELECT * FROM gate_results")
    reasons = json.loads(gate["reasons"])
    assert gate["plan_id"] == plan["plan_id"] and gate["verdict"] == "APPROVED"
    assert reasons["sized"]["risk_usd"] > 0 and reasons["sized"]["notional_usd"] > 0
    assert any(r["name"] == "manual" for r in reasons["reasons"]), "the bypass must be on record"

    (order,) = _rows("SELECT * FROM orders")
    assert order["run_id"] == run["run_id"] and order["plan_id"] == plan["plan_id"]
    assert order["cl_ord_id"] == client_order_id(run["run_id"], BTC, "long")
    assert order["kind"] == "entry" and order["exchange_id"]
    assert order_state(open_journal(), order["order_id"]) == "OPEN"


def test_the_next_managers_book_can_read_the_manual_entry(armed: None, venue: Venue) -> None:
    """`_resting` raises on an entry with no recorded size - that would fail every later run."""
    from argus.runs.manager import _resting, gate_book

    assert _run(*ARGS, "--yes")[0] == 0
    engine = open_journal()
    (resting,) = _resting(engine)
    assert resting.symbol == "BTC" and resting.side == "long"
    assert [p.symbol for p in gate_book(engine, [])] == ["BTC"]


# ── the watchdog ──────────────────────────────────────────────────────────────────────────────


def test_the_watchdog_sees_it_as_owned_with_its_stop(
    armed: None, venue: Venue, tmp_path: Path
) -> None:
    assert _run(*ARGS, "--yes")[0] == 0
    fake = venue.fake
    (entry_id,) = [o.order_id for o in fake.orders.values()]
    fake.fill(entry_id)  # the venue fills a market order at once; the fake does it on request

    engine = open_journal()
    settings = Settings(mode="approve", live_trading="I_UNDERSTAND", okx_demo=True)
    watchdog = Watchdog(
        exchange=fake,
        engine=engine,
        notifier=Notifier(bot=RecordingBot(), chat_id="42"),
        settings=settings,
    )
    with start_run(engine, "watchdog", "demo") as run_id:
        result = watchdog.run_once(run_id)

    assert result.positions == 1
    assert result.discrepancies == [], "an orphan, ghost or size drift on a manual trade"
    assert result.repaired == [] and result.repair_failures == []
    assert fake.reconcile()["unprotected"] == []
    assert plan_stops_from_journal(engine, [], venue="demo") == {("btc", "long"): Decimal("73125")}
    assert not _rows("SELECT * FROM position_events WHERE to_state = 'ORPHAN'")


# ── the other guards ──────────────────────────────────────────────────────────────────────────


def test_it_will_not_add_to_a_position_the_venue_already_holds(armed: None, venue: Venue) -> None:
    venue.fake.open_unprotected(BTC, side="short")
    code, out = _run(*ARGS, "--yes")
    assert code == 1 and "BTC" in out
    assert venue.fake.orders == {}


def test_running_it_twice_does_not_stack_a_second_position(armed: None, venue: Venue) -> None:
    assert _run(*ARGS, "--yes")[0] == 0
    (entry_id,) = [o.order_id for o in venue.fake.orders.values()]
    venue.fake.fill(entry_id)
    code, _ = _run(*ARGS, "--yes")
    assert code == 1
    assert len(venue.fake.orders) == 1


def test_it_will_not_place_under_a_halt(armed: None, venue: Venue) -> None:
    settings = config.get_settings()
    settings.halt_file.parent.mkdir(parents=True, exist_ok=True)
    settings.halt_file.write_text("operator halt")
    code, out = _run(*ARGS, "--yes")
    assert code == 1 and "HALT" in out
    assert venue.fake.orders == {}


def test_it_takes_the_run_lock_and_waits_for_a_run_in_progress(
    armed: None, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "MANUAL_LOCK_WAIT_S", 0.2)
    with run_lock("manager", config.get_settings().argus_home):
        code, out = _run(*ARGS, "--yes")
    assert code == 1
    assert "manager" in out
    assert venue.fake.orders == {}


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        (["--stop-pct", "0"], "stop-pct"),
        (["--stop-pct", "40"], "stop-pct"),
        (["--target-pct", "0"], "target-pct"),
        (["--side", "up"], "side"),
        (["--symbol", "BTC-USDT-SWAP"], "symbol"),
    ],
)
def test_nonsense_levels_are_refused_before_anything_is_built(
    armed: None, venue: Venue, extra: list[str], named: str
) -> None:
    code, out = _run(*ARGS, *extra, "--yes")
    assert code == 2 and named in out, out
    assert venue.built == 0 and venue.fake.orders == {}


def test_it_prints_how_to_close_it(armed: None, venue: Venue) -> None:
    code, out = _run(*ARGS, "--yes")
    assert code == 0, out
    # One instrument: without --symbol, flatten writes a HALT and closes the whole account.
    assert "flatten --symbol BTC-USDT-SWAP --yes" in out


def test_the_journal_fixture_is_the_one_the_command_wrote(armed: None, venue: Venue) -> None:
    """Guard on the tests themselves: they read the journal the CLI wrote, not a stray one."""
    assert _run(*ARGS, "--yes")[0] == 0
    engine: Engine = open_journal()
    assert engine.url.database is not None
    assert str(config.get_settings().argus_home) in engine.url.database
