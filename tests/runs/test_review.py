"""The review run. Every test here is about what it refuses to do to an open position."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from argus.journal.db import open_journal
from argus.llm.router import AllModelsFailedError
from argus.llm.schemas import ReviewDecision
from argus.runs.review import run_review


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """A journal with the review run `r1` open, as `start_run` leaves it in production."""
    from sqlalchemy import text

    eng = open_journal(tmp_path / "journal.db")
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                " VALUES ('r1','review','approve','2026-10-07T00:00:00+00:00','RUNNING','demo')"
            )
        )
    return eng


class FakeVenue:
    def __init__(
        self,
        positions: list[dict[str, Any]] | None = None,
        stop: float = 96.0,
        algos: list[dict[str, Any]] | Exception | None = None,
    ) -> None:
        self.positions = positions if positions is not None else [{"symbol": "SOL", "side": "long"}]
        self.stop = stop
        self.algos = algos
        self.did: list[tuple[str, Any]] = []

    def fetch_positions(self) -> list[dict[str, Any]]:
        return self.positions

    def fetch_algo_orders(self) -> list[dict[str, Any]]:
        if isinstance(self.algos, Exception):
            raise self.algos
        if self.algos is not None:
            return self.algos
        return [{"symbol": "SOL", "kind": "sl", "trigger_px": self.stop}]

    def close(self, symbol: str) -> str:
        self.did.append(("close", symbol))
        return "x"

    def partial_close(self, symbol: str, pct: int) -> str:
        self.did.append(("trim", pct))
        return "x"

    def amend_stop(self, symbol: str, new_stop: Decimal) -> str:
        self.did.append(("amend", float(new_stop)))
        return "x"


class _Call:
    def __init__(self, output: Any) -> None:
        self.output = output


def _decide(action: str, **over: Any) -> ReviewDecision:
    base: dict[str, Any] = {"action": action, "side": "long", "rationale": "because"}
    if action == "MOVE_STOP":
        base |= {"current_stop": 96.0, "new_stop": 98.0}
    if action == "TRIM":
        base |= {"trim_pct": 50}
    return ReviewDecision(**(base | over))


def _patch(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    from argus.runs import review as module

    def fake(*_a: Any, **_k: Any) -> Any:
        if isinstance(answer, Exception):
            raise answer
        return _Call(answer)

    monkeypatch.setattr(module, "ask_reviewer", fake)


def _dossier(_symbol: str) -> Any:
    class _D:
        derivatives: dict[str, Any] = {}
        technical: dict[str, Any] = {}

    return _D()


def test_a_flat_book_asks_nothing(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """No positions, no model call. A review of nothing costs nothing."""
    _patch(monkeypatch, _decide("CLOSE"))
    venue = FakeVenue(positions=[])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert result.positions == 0
    assert venue.did == []


class _Untouchable:
    """A venue that fails the test on any use at all."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the review touched the venue ({name}) under a HALT")


def test_a_halt_does_not_stop_a_dry_run(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dry run sends nothing, so it may still show what the review would have done."""
    from argus.runs.watchdog import write_halt

    _patch(monkeypatch, _decide("CLOSE"))
    halt = tmp_path / "HALT"
    write_halt(halt, "operator: looking")
    venue = FakeVenue()
    result = run_review(engine, object(), venue, "r1", _dossier, True, halt_file=halt, venue="demo")
    assert result.outcomes[0].action == "CLOSE"
    assert venue.did == []


def test_a_dry_run_decides_but_sends_nothing(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, _decide("CLOSE"))
    venue = FakeVenue()
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=True, venue="demo")
    assert result.outcomes[0].action == "CLOSE"
    assert not result.outcomes[0].applied
    assert venue.did == []


def test_close_and_trim_reach_the_venue(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _decide("CLOSE"))
    venue = FakeVenue()
    run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == [("close", "SOL")]

    _patch(monkeypatch, _decide("TRIM", trim_pct=40))
    venue = FakeVenue()
    run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == [("trim", 40)]


def test_a_stop_may_be_tightened(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=96.0, new_stop=98.0))
    venue = FakeVenue(stop=96.0)
    run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == [("amend", 98.0)]


def test_a_widening_stop_is_refused_by_the_schema() -> None:
    """The first of two locks: the model cannot even express it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="never widened"):
        _decide("MOVE_STOP", current_stop=96.0, new_stop=90.0)


def test_a_stop_is_rechecked_against_the_venue_not_the_dossier(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second lock, and the one that matters in production.

    The schema compares against the stop the model was SHOWN. If anything moved the stop since
    that dossier was built — a trail, an earlier review — the venue's stop is higher, and an
    amend that looked like a tightening is really a widening.
    """
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=96.0, new_stop=98.0))
    venue = FakeVenue(stop=99.0)  # already tighter than the model's proposal
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")

    assert venue.did == [], "nothing may reach the venue"
    assert not result.outcomes[0].applied
    assert "does not tighten" in result.outcomes[0].detail


def test_an_unreachable_reviewer_leaves_the_position_alone(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safety criticality. A position nobody could judge is not one everybody agreed to hold."""
    _patch(monkeypatch, AllModelsFailedError("every model failed"))
    venue = FakeVenue()
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")

    assert venue.did == []
    assert result.outcomes[0].action == "UNREVIEWED"
    assert result.errors


def test_one_bad_position_does_not_stop_the_others(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus.runs import review as module

    seen: list[str] = []

    def fake(_router: Any, _run: str, position: dict[str, Any], *_a: Any, **_k: Any) -> Any:
        seen.append(position["symbol"])
        if position["symbol"] == "SOL":
            raise RuntimeError("dossier exploded")
        return _Call(_decide("CLOSE"))

    monkeypatch.setattr(module, "ask_reviewer", fake)
    venue = FakeVenue(
        positions=[{"symbol": "SOL", "side": "long"}, {"symbol": "ETH", "side": "long"}]
    )
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")

    assert seen == ["SOL", "ETH"]
    assert ("close", "ETH") in venue.did
    assert any("SOL" in e for e in result.errors)


def test_the_review_vocabulary_cannot_open_a_position() -> None:
    """There is no verb for it, and no size field anywhere in the contract."""
    import typing

    actions = typing.get_args(ReviewDecision.model_fields["action"].annotation)
    assert set(actions) == {"HOLD", "CLOSE", "TRIM", "MOVE_STOP"}
    assert "size" not in ReviewDecision.model_fields
    assert "leverage" not in ReviewDecision.model_fields


def _seed_plan(
    engine: Engine,
    symbol: str = "SOL",
    side: str = "long",
    order: str | tuple[str, ...] | None = "OPEN",
    at: str = "2026-09-25T10:00:00+00:00",
    payload: dict[str, Any] | None = None,
    reason: str = "seeded",
) -> str:
    """A run and the plan that opened the position, as the manager would have written them.

    `order` is the state the plan's entry order reached through `place_approved` — OPEN once the
    venue accepted it, REJECTED when it refused — or None for a plan that was never placed (a
    shadow run, or a gate verdict nobody executed). A tuple walks the order through each state in
    turn; `reason` is written on the last step. The order is journalled at `at`.
    """
    import json
    from unittest.mock import patch

    from sqlalchemy import text

    from argus.execution.lifecycle import record_order, transition_order
    from argus.journal.db import new_id

    run_id, plan_id = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                " VALUES (:r,'manager','approve',:t,'OK','demo')"
            ),
            {"r": run_id, "t": at},
        )
        conn.execute(
            text(
                "INSERT INTO plans (plan_id, run_id, created_at, mode, symbol, side, state,"
                " payload, prev_hash, row_hash) VALUES (:p,:r,:t,"
                " 'approve',:s,:side,'GATED',:payload,'','')"
            ),
            {
                "p": plan_id,
                "r": run_id,
                "t": at,
                "s": symbol,
                "side": side,
                "payload": json.dumps(
                    payload
                    or {
                        "entry": [99.8, 100.2],
                        "stop": 96.0,
                        "thesis": "EMA reclaim",
                        "atr_14": 4.0,
                    }
                ),
            },
        )
    if order is None:
        return plan_id
    path = (order,) if isinstance(order, str) else order
    with patch("argus.journal.ledger.utc_now", return_value=at):
        order_id = record_order(
            engine,
            run_id,
            {
                "cl_ord_id": (run_id[:8] + plan_id[:8]),
                "symbol": f"{symbol}-USDT-SWAP",
                "side": "buy" if side == "long" else "sell",
                "order_type": "limit",
                "price": "100",
                "size": "1",
            },
            plan_id=plan_id,
        )
        for i, state in enumerate(path):
            last = i == len(path) - 1
            transition_order(engine, order_id, state, reason=reason if last else "seeded")
    return plan_id


class _Closed:
    def __init__(self, symbol: str, entry: float, side: str, mark: float) -> None:
        self.symbol, self.side = symbol, side
        self.entry_price = entry
        self.last_seen = {"mark_price": mark, "entry_price": entry}


def test_a_post_mortem_is_written_for_a_closed_trade(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store_postmortem` had no production caller, so closed trades reached the Reflector with
    no verdict attached — leaving it to infer process quality from PnL, the exact bias the
    Post-mortem prompt spends most of its length preventing (F-132)."""
    from sqlalchemy import text

    from argus.llm.schemas import PostMortem
    from argus.runs import review as module

    plan_id = _seed_plan(engine)
    report = PostMortem(
        process_verdict="sound",
        would_take_again=True,
        entry_timing="good",
        sizing="right",
        stop_placement="right",
        missed_signals=[],
        do_differently="",
        feedback="The thesis held; price reached the stop first.",
    )
    monkeypatch.setattr(module, "AllModelsFailedError", AllModelsFailedError)
    monkeypatch.setattr("argus.llm.agents.postmortem.analyse", lambda *a, **k: _Call(report))

    written = module.write_postmortems(
        engine, object(), "r-review", [_Closed("SOL", 100.0, "long", 96.0)], venue="demo"
    )
    assert written == ["SOL: sound"]

    with engine.connect() as conn:
        row = (
            conn.execute(text("SELECT plan_id, outcome_r, process_verdict FROM postmortems"))
            .mappings()
            .first()
        )
    assert row["plan_id"] == plan_id, "must reference the real plan, not a synthesised id"
    # Entry 100, stop 96, exit 96 -> exactly -1R, computed from the risk the trade was SIZED
    # against rather than from the venue's PnL, whose denominator nobody decided.
    assert row["outcome_r"] == -1.0
    assert row["process_verdict"] == "sound"


def test_a_position_with_no_plan_is_not_post_mortemed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Judging the process behind a position we never opened would be judging nothing."""
    from argus.runs import review as module

    called: list[str] = []
    monkeypatch.setattr(
        "argus.llm.agents.postmortem.analyse",
        lambda *a, **k: called.append("asked") or _Call(None),
    )
    assert (
        module.write_postmortems(
            engine, object(), "r1", [_Closed("DOGE", 1.0, "long", 0.9)], venue="demo"
        )
        == []
    )
    assert called == []


def test_a_dry_run_judges_but_stores_nothing(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    from argus.llm.schemas import PostMortem
    from argus.runs import review as module

    _seed_plan(engine)
    report = PostMortem(
        process_verdict="flawed",
        would_take_again=False,
        entry_timing="late",
        sizing="right",
        stop_placement="too_tight",
        missed_signals=[],
        do_differently="Place the stop beyond the level.",
        feedback="Chased.",
    )
    monkeypatch.setattr("argus.llm.agents.postmortem.analyse", lambda *a, **k: _Call(report))

    written = module.write_postmortems(
        engine, object(), "r1", [_Closed("SOL", 100.0, "long", 97.0)], dry_run=True, venue="demo"
    )
    assert "not stored" in written[0]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM postmortems")).scalar() == 0


# --- R-1 / L-1: the tighten-only check reads the venue, never the model ----------------------
#
# Review 2026-10-07. Four gaps let MOVE_STOP widen a live stop: the side came from the model, an
# unreadable trigger parsed as 0.0, a failed algo fetch skipped the check, and the stop was keyed
# by OKX's instId while positions arrive ccxt-spelled — so in production the check never ran.
# Every shape below is what `OKXAdapter.fetch_positions` / `fetch_algo_orders` actually return.

BTC_POSITION: dict[str, Any] = {
    "symbol": "BTC/USDT:USDT",
    "side": "long",
    "size": Decimal("1"),
    "entry_price": Decimal("100000"),
    "mark_price": Decimal("101000"),
    "leverage": 3,
    "liq_price": Decimal("70000"),
    "unrealised": Decimal("1000"),
    "margin_ratio": None,
}


def _okx_algo(kind: str, trigger: str | None, inst: str = "BTC-USDT-SWAP") -> dict[str, Any]:
    return {
        "algo_id": f"{kind}-1",
        "symbol": inst,
        "kind": kind,
        "trigger_px": None if trigger is None else Decimal(trigger),
        "callback_pct": Decimal("1") if kind == "trailing" else None,
        "size": Decimal("1"),
        "state": "live",
    }


def _btc_venue(algos: list[dict[str, Any]] | Exception, side: str = "long") -> FakeVenue:
    return FakeVenue(positions=[{**BTC_POSITION, "side": side}], algos=algos)


def test_the_venue_stop_is_found_across_symbol_spellings(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Position `BTC/USDT:USDT`, algo `BTC-USDT-SWAP`. The model was shown a stale 90 000; the
    venue's stop is 98 000, so 95 000 is a widening and must not be sent."""
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=90000.0, new_stop=95000.0))
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == []
    assert "does not tighten" in result.outcomes[0].detail


def test_the_side_comes_from_the_venue_not_the_model(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's own scenario: a long with its stop at 98 000, a model that says `short`.
    Read as a short, 95 000 "tightens" 98 000; on the long it actually is, it widens by 3%."""
    _patch(
        monkeypatch,
        _decide("MOVE_STOP", side="short", current_stop=99000.0, new_stop=95000.0),
    )
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == []
    assert "short" in result.outcomes[0].detail and "long" in result.outcomes[0].detail


@pytest.mark.parametrize(
    "algos",
    [
        pytest.param(RuntimeError("429 on ordType=conditional"), id="algo fetch failed"),
        pytest.param([], id="no stop at all"),
        pytest.param([_okx_algo("sl", "98000", inst="ETH-USDT-SWAP")], id="stop on another asset"),
        pytest.param([_okx_algo("sl", None)], id="trigger unreadable"),
        pytest.param([_okx_algo("sl", "0")], id="trigger zero"),
    ],
)
def test_move_stop_is_refused_when_the_venue_stop_is_unknown(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    algos: list[dict[str, Any]] | Exception,
) -> None:
    """Unknown is not 0.0 (J-N11), and an unanswered question is not permission. Any positive
    stop "tightened" 0.0, and a failed fetch skipped the comparison altogether."""
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=90000.0, new_stop=95000.0))
    venue = _btc_venue(algos)
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == []
    assert not result.outcomes[0].applied
    assert "unknown" in result.outcomes[0].detail


def test_a_trailing_stop_makes_the_venue_stop_unverifiable(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trailing stop's live trigger moves with price and the adapter does not report it, so
    with one resting the binding stop cannot be known — and an amend cannot be shown to tighten."""
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=96000.0, new_stop=98000.0))
    venue = _btc_venue([_okx_algo("sl", "96000"), _okx_algo("trailing", None)])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == []
    assert "unknown" in result.outcomes[0].detail


def test_a_trim_below_the_minimum_size_does_not_fail_the_run(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26: the Reviewer asked to trim a minimum-size BTC position; the venue cannot, and
    the whole review exited 1. Nothing is wrong: the position simply stays as it is."""
    from argus.exchange.errors import TrimTooSmallError

    venue = FakeVenue()

    def too_small(symbol: str, pct: int) -> str:
        raise TrimTooSmallError(f"{pct}% of {symbol} is below the minimum size")

    monkeypatch.setattr(venue, "partial_close", too_small)
    _patch(monkeypatch, _decide("TRIM", trim_pct=50))
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert result.errors == []
    assert [o.status for o in result.outcomes] == ["refused"]
    assert "minimum size" in result.outcomes[0].detail


def test_close_and_trim_still_act_when_the_stop_is_unknown(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only MOVE_STOP needs the venue's stop. Reducing risk must not wait on it."""
    _patch(monkeypatch, _decide("CLOSE"))
    venue = _btc_venue(RuntimeError("algo fetch failed"))
    run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == [("close", "BTC/USDT:USDT")]


def test_a_real_tightening_on_okx_spelled_rows_reaches_the_venue(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=90000.0, new_stop=99000.0))
    venue = _btc_venue([_okx_algo("sl", "98000"), _okx_algo("tp", "120000")])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == [("amend", 99000.0)]
    assert result.outcomes[0].applied


def test_a_short_is_checked_as_a_short(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch, _decide("MOVE_STOP", side="short", current_stop=110000.0, new_stop=103000.0)
    )
    venue = _btc_venue([_okx_algo("sl", "102000")], side="short")
    run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == [], "103 000 is above a short's 102 000 stop: a widening"

    _patch(
        monkeypatch, _decide("MOVE_STOP", side="short", current_stop=110000.0, new_stop=101000.0)
    )
    venue = _btc_venue([_okx_algo("sl", "102000")], side="short")
    run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == [("amend", 101000.0)]


@pytest.mark.parametrize(
    ("side", "stops", "model_stop", "between"),
    [
        pytest.param("long", ("95000", "98000"), 90000.0, 96500.0, id="long"),
        pytest.param("short", ("105000", "102000"), 110000.0, 103500.0, id="short"),
    ],
)
def test_with_two_stops_the_one_that_fires_first_binds(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    stops: tuple[str, str],
    model_stop: float,
    between: float,
) -> None:
    """Two stop algos on one asset, and a move that lands between them: it tightens the looser
    stop and widens the one that fires first. No test had two stops, so taking the loosest as
    binding (max and min swapped in `binding_stop`) passed them all (second review, R-1)."""
    _patch(monkeypatch, _decide("MOVE_STOP", side=side, current_stop=model_stop, new_stop=between))
    venue = _btc_venue([_okx_algo("sl", level) for level in stops], side=side)
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert venue.did == []
    assert result.outcomes[0].status == "refused"
    assert f"does not tighten the venue's stop {stops[1]}" in result.outcomes[0].detail


def test_a_dry_run_shows_the_stop_move_a_live_run_would_refuse(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dry-run` skipped `_apply`, venue checks and all, so a widening a live run refuses was
    journalled "dry_run" like any other decision: demo dry runs could never show a refusal."""
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=90000.0, new_stop=95000.0))
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=True, venue="demo")

    assert venue.did == []
    outcome = result.outcomes[0]
    assert outcome.status == "refused" and not outcome.applied
    assert "does not tighten the venue's stop 98000" in outcome.detail
    event = _review_events(engine)[0]
    assert event["detail"]["status"] == "refused" and event["detail"]["dry_run"] is True


def test_a_dry_run_records_a_stop_move_a_live_run_would_send_as_dry_run(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=90000.0, new_stop=99000.0))
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=True, venue="demo")
    assert venue.did == []
    assert result.outcomes[0].status == "dry_run" and not result.errors


def test_the_model_is_shown_the_venue_stop_and_its_own_number_is_replaced(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`position_row` never carried the stop, so the model guessed it — and the schema's own
    tighten-only check compared two guesses. It is shown the venue's level now, and whatever it
    writes back as `current_stop` is replaced by that level before anything is recorded."""
    from argus.runs import review as module

    shown: list[dict[str, Any]] = []

    def fake(_router: Any, _run: str, position: dict[str, Any], *_a: Any, **_k: Any) -> Any:
        shown.append(dict(position))
        return _Call(_decide("MOVE_STOP", current_stop=90000.0, new_stop=99000.0))

    monkeypatch.setattr(module, "ask_reviewer", fake)
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")

    assert shown[0]["current_stop"] == 98000.0
    assert result.outcomes[0].decision is not None
    assert result.outcomes[0].decision.current_stop == 98000.0


# --- J-N10: the plan judged is the plan that was placed ----------------------------------------


def _postmortem_plan_id(engine: Engine, monkeypatch: pytest.MonkeyPatch, closed: Any) -> Any:
    """Write one post-mortem and return the plan id it was filed against (None if skipped)."""
    from sqlalchemy import text

    from argus.llm.schemas import PostMortem
    from argus.runs import review as module

    report = PostMortem(
        process_verdict="sound",
        would_take_again=True,
        entry_timing="good",
        sizing="right",
        stop_placement="right",
        missed_signals=[],
        do_differently="",
        feedback="ok",
    )
    monkeypatch.setattr("argus.llm.agents.postmortem.analyse", lambda *a, **k: _Call(report))
    module.write_postmortems(engine, object(), "r-review", [closed], venue="demo")
    with engine.connect() as conn:
        return conn.execute(text("SELECT plan_id FROM postmortems")).scalar()


def test_a_newer_plan_that_was_never_placed_is_not_the_one_judged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shadow run, or a gate verdict nobody executed, writes a GATED plan for the same asset
    after the real one. The newest non-REJECTED plan was that one — a thesis that never traded."""
    placed = _seed_plan(engine, at="2026-09-25T10:00:00+00:00")
    _seed_plan(engine, order=None, at="2026-09-26T10:00:00+00:00")
    closed = _Closed("SOL", 100.0, "long", 96.0)
    assert _postmortem_plan_id(engine, monkeypatch, closed) == placed


def test_a_plan_the_venue_refused_is_not_the_one_judged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    placed = _seed_plan(engine, at="2026-09-25T10:00:00+00:00")
    _seed_plan(engine, order="REJECTED", at="2026-09-26T10:00:00+00:00")
    closed = _Closed("SOL", 100.0, "long", 96.0)
    assert _postmortem_plan_id(engine, monkeypatch, closed) == placed


def test_the_plan_judged_is_on_the_position_s_side(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Net mode holds one position per asset; a newer placed SHORT plan is not the long's plan."""
    placed = _seed_plan(engine, side="long", at="2026-09-25T10:00:00+00:00")
    _seed_plan(engine, side="short", at="2026-09-26T10:00:00+00:00")
    closed = _Closed("SOL", 100.0, "long", 96.0)
    assert _postmortem_plan_id(engine, monkeypatch, closed) == placed


def test_a_ccxt_spelled_position_finds_its_asset_keyed_plan(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plans are keyed `SOL`; OKX positions arrive `SOL/USDT:USDT` (review R-1, runs view)."""
    placed = _seed_plan(engine)
    closed = _Closed("SOL/USDT:USDT", 100.0, "long", 96.0)
    assert _postmortem_plan_id(engine, monkeypatch, closed) == placed


def test_only_never_placed_plans_means_no_plan(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_plan(engine, order=None)
    closed = _Closed("SOL", 100.0, "long", 96.0)
    assert _postmortem_plan_id(engine, monkeypatch, closed) is None


def test_the_reviewer_is_shown_the_placed_plan(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus.runs import review as module

    _seed_plan(engine, payload={"stop": 96.0, "thesis": "the placed one"})
    _seed_plan(engine, order=None, payload={"stop": 90.0, "thesis": "a shadow plan"})
    plans: list[dict[str, Any]] = []

    def fake(_r: Any, _run: str, _pos: Any, plan: dict[str, Any], *_a: Any, **_k: Any) -> Any:
        plans.append(plan)
        return _Call(_decide("HOLD"))

    monkeypatch.setattr(module, "ask_reviewer", fake)
    venue = FakeVenue(positions=[{"symbol": "SOL/USDT:USDT", "side": "long"}])
    run_review(engine, object(), venue, "r1", _dossier, dry_run=False, venue="demo")
    assert plans[0]["thesis"] == "the placed one"


# Second review of J-N10: no time bound, and a cancelled or expired entry that filled in part lost
# to any older filled one — so an open position could be judged against a trade closed long ago.
PARTIAL_THEN_EXPIRED = ("OPEN", "PARTIAL", "EXPIRED")


def _thesis_shown(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, opened_at: str | None
) -> str | None:
    from argus.runs import review as module

    plans: list[dict[str, Any]] = []

    def fake(_r: Any, _run: str, _pos: Any, plan: dict[str, Any], *_a: Any, **_k: Any) -> Any:
        plans.append(plan)
        return _Call(_decide("HOLD"))

    monkeypatch.setattr(module, "ask_reviewer", fake)
    row = {"symbol": "SOL/USDT:USDT", "side": "long", "opened_at": opened_at}
    run_review(
        engine, object(), FakeVenue(positions=[row]), "r1", _dossier, dry_run=False, venue="demo"
    )
    return plans[0].get("thesis")


def test_an_entry_that_filled_in_part_then_expired_is_the_plan_judged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resting sweep expires a part-filled entry (state EXPIRED, reason "partially filled …")
    and the position it opened stays. An older FILLED entry from a long-closed trade won, and its
    targets, horizon and kill rules were the ones the open position was judged by."""
    _seed_plan(
        engine,
        order=("OPEN", "FILLED"),
        at="2026-09-01T10:00:00+00:00",
        payload={"stop": 90.0, "thesis": "a trade closed weeks ago"},
    )
    _seed_plan(
        engine,
        order=PARTIAL_THEN_EXPIRED,
        at="2026-10-01T10:00:00+00:00",
        payload={"stop": 96.0, "thesis": "the part-filled entry"},
        reason="partially filled 0.4; unfilled for 30.0h (valid 24h)",
    )
    shown = _thesis_shown(engine, monkeypatch, opened_at="2026-10-01T12:00:00+00:00")
    assert shown == "the part-filled entry"


def test_a_filled_entry_is_preferred_to_one_that_ended_unfilled(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the preference: an entry cancelled with nothing filled opened nothing,
    so the older filled one is the plan behind the position."""
    _seed_plan(
        engine,
        order=("OPEN", "FILLED"),
        at="2026-10-01T10:00:00+00:00",
        payload={"stop": 96.0, "thesis": "the filled entry"},
    )
    _seed_plan(
        engine,
        order=("OPEN", "CANCELLED"),
        at="2026-10-02T10:00:00+00:00",
        payload={"stop": 97.0, "thesis": "cancelled unfilled"},
        reason="drift: last 110 is more than 2% beyond entry 100",
    )
    assert _thesis_shown(engine, monkeypatch, opened_at=None) == "the filled entry"


def test_an_entry_placed_after_the_position_opened_is_not_its_plan(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The venue says when the position opened; its entry was journalled before that. A newer
    resting entry on the same asset and side is somebody else's plan."""
    _seed_plan(
        engine,
        order=("OPEN", "FILLED"),
        at="2026-10-01T10:00:00+00:00",
        payload={"stop": 96.0, "thesis": "the opener"},
    )
    _seed_plan(
        engine,
        order="OPEN",
        at="2026-10-03T10:00:00+00:00",
        payload={"stop": 90.0, "thesis": "a newer resting entry"},
    )
    shown = _thesis_shown(engine, monkeypatch, opened_at="2026-10-01T10:02:00+00:00")
    assert shown == "the opener"


def _snapshot(engine: Engine, at: str, symbol: str = "SOL/USDT:USDT") -> None:
    """The watchdog seeing the position open at `at`."""
    from argus.journal import positions

    row = {"symbol": symbol, "side": "long", "entry_price": 100.0, "mark_price": 96.0}
    positions.record(engine, "r1", [row], now=datetime.fromisoformat(at))


def _closes(engine: Engine, at: str) -> list[Any]:
    """The venue no longer reporting it; returns what `newly_closed` hands the post-mortems."""
    from argus.journal import positions

    closed = positions.newly_closed(engine, [], venue="demo")
    positions.mark_closed(engine, "r1", closed, now=datetime.fromisoformat(at))
    return closed


def test_a_closed_trade_is_not_judged_by_a_newer_resting_entry(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop fills; the manager places the next entry on the same asset before the close is
    noticed. The newest live entry was that one — its stop gave the wrong R, and its own
    post-mortem later overwrote this one, since `store_postmortem` replaces on plan_id."""
    from argus.journal import positions

    traded = _seed_plan(engine, order=("OPEN", "FILLED"), at="2026-10-01T10:00:00+00:00")
    _snapshot(engine, "2026-10-01T12:00:00+00:00")
    _seed_plan(engine, order="OPEN", at="2026-10-01T14:00:00+00:00", payload={"stop": 80.0})
    closed = positions.newly_closed(engine, [], venue="demo")
    assert _postmortem_plan_id(engine, monkeypatch, closed[0]) == traded


def test_a_trade_s_entry_placed_before_the_last_close_was_noticed_is_still_its_plan(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trade one is last seen open at 12:00 and its close noticed at 16:00; trade two's entry went
    in at 14:00, in between. Bounding by the previous CLOSED row would lose trade two's plan."""
    from argus.journal import positions

    _seed_plan(engine, order=("OPEN", "FILLED"), at="2026-10-01T10:00:00+00:00")
    _snapshot(engine, "2026-10-01T12:00:00+00:00")
    second = _seed_plan(engine, order=("OPEN", "FILLED"), at="2026-10-01T14:00:00+00:00")
    _closes(engine, "2026-10-01T16:00:00+00:00")
    _snapshot(engine, "2026-10-01T18:00:00+00:00")
    closed = positions.newly_closed(engine, [], venue="demo")
    assert _postmortem_plan_id(engine, monkeypatch, closed[0]) == second


def test_a_position_opened_by_hand_is_not_judged_by_the_last_trade_s_plan(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing this system placed opened the second position, so there is no plan to judge — not
    the first trade's, which closed before it began."""
    from argus.journal import positions

    _seed_plan(engine, order=("OPEN", "FILLED"), at="2026-10-01T10:00:00+00:00")
    _snapshot(engine, "2026-10-01T12:00:00+00:00")
    _closes(engine, "2026-10-01T16:00:00+00:00")
    _snapshot(engine, "2026-10-02T12:00:00+00:00")
    closed = positions.newly_closed(engine, [], venue="demo")
    assert _postmortem_plan_id(engine, monkeypatch, closed[0]) is None


# --- L-3 / D-N1: the deterministic flags can fire on what production actually passes ----------

# Exactly what `manager._record_decision` journals — lists for targets, typed kill rules.
MANAGER_PAYLOAD: dict[str, Any] = {
    "entry": [99800.0, 100200.0],
    "entry_kind": "limit",
    "stop": 96000.0,
    "targets": [[112000.0, 50], [124000.0, 50]],
    "time_stop_hours": 72,
    "scores": {"fundamental": 0, "onchain": 0, "macro": 0, "catalyst": 1, "technical": 2},
    "invalidation_rules": [{"kind": "price_close_below", "level": 92000.0, "timeframe": "4h"}],
    "thesis": "trend continuation",
    "dossier_id": "d1",
}


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from argus.runs import review as module

    seen: list[dict[str, Any]] = []

    def fake(
        _r: Any, _run: str, position: Any, plan: Any, _d: Any, flags: Any, *_a: Any, **_k: Any
    ) -> Any:
        seen.append({"position": dict(position), "plan": plan, "flags": dict(flags)})
        return _Call(_decide("HOLD"))

    monkeypatch.setattr(module, "ask_reviewer", fake)
    return seen


def _priced(price: float) -> Any:
    def dossier_for(_symbol: str) -> Any:
        class _D:
            derivatives: dict[str, Any] = {}
            technical: dict[str, Any] = {"price": price}

        return _D()

    return dossier_for


def test_a_breached_horizon_reaches_the_reviewer_flagged(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 72 h plan, ten days on. `position_row` never carried `hours_in_trade`, so the flag
    read 0 hours and the position was HOLD-able forever."""
    _seed_plan(engine, symbol="BTC", payload=MANAGER_PAYLOAD)
    seen = _capture(monkeypatch)
    venue = _btc_venue([_okx_algo("sl", "96000")])
    later = datetime.now(UTC) + timedelta(days=10)
    run_review(
        engine, object(), venue, "r1", _priced(99000.0), dry_run=False, now=later, venue="demo"
    )

    assert seen[0]["flags"]["time_stop_breached"] is True
    assert seen[0]["position"]["hours_in_trade"] >= 240


def test_the_venue_s_own_open_time_is_preferred(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan was journalled just now, but OKX says the position has been open for 100 h."""
    _seed_plan(engine, symbol="BTC", payload=MANAGER_PAYLOAD)
    seen = _capture(monkeypatch)
    now = datetime.now(UTC)
    opened = (now - timedelta(hours=100)).isoformat()
    venue = FakeVenue(
        positions=[{**BTC_POSITION, "opened_at": opened}], algos=[_okx_algo("sl", "96000")]
    )
    run_review(
        engine, object(), venue, "r1", _priced(99000.0), dry_run=False, now=now, venue="demo"
    )

    assert round(seen[0]["position"]["hours_in_trade"]) == 100
    assert seen[0]["flags"]["time_stop_breached"] is True


def test_invalidation_and_liquidation_flags_fire_on_okx_shaped_rows(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mark 101 000, liquidation 70 000, stop 85 000: the stop sits over halfway to liquidation.
    The plan said a 4h close under 92 000 kills it, and the last close is 91 000."""
    _seed_plan(engine, symbol="BTC", payload=MANAGER_PAYLOAD)
    seen = _capture(monkeypatch)
    venue = _btc_venue([_okx_algo("sl", "85000")])
    run_review(engine, object(), venue, "r1", _priced(91000.0), dry_run=False, venue="demo")

    flags = seen[0]["flags"]
    assert flags["invalidation_hit"] is True
    assert flags["stop_near_liquidation"] is True
    assert flags["tp1_hit"] is False


def test_a_dossier_with_no_price_is_flagged_stale(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing ever set `technical["stale"]`, so `data_stale` could not fire in production. A
    review with no closed price behind it is judging on data it does not have."""
    seen = _capture(monkeypatch)
    run_review(engine, object(), _btc_venue([]), "r1", _dossier, dry_run=False, venue="demo")
    assert seen[0]["flags"]["data_stale"] is True

    seen.clear()
    run_review(
        engine, object(), _btc_venue([]), "r1", _priced(99000.0), dry_run=False, venue="demo"
    )
    assert seen[0]["flags"]["data_stale"] is False


@pytest.mark.parametrize(("closed_ago_h", "stale"), [(13.0, True), (3.0, False)])
def test_a_price_that_is_there_but_old_is_flagged_stale(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, closed_ago_h: float, stale: bool
) -> None:
    """A lagging feed or a cached frame still carries a 4h close; only a missing one was flagged,
    so the `stale_price_feed` case the Reviewer is measured on to HOLD never arose in production.
    Fresh, the last closed 4h bar closed under 4 h ago; 13 h means two bars never arrived."""
    from types import SimpleNamespace

    seen = _capture(monkeypatch)
    now = datetime.now(UTC)
    closed_at = (now - timedelta(hours=closed_ago_h)).isoformat()
    dossier = SimpleNamespace(derivatives={}, technical={"price": 99000.0, "price_at": closed_at})
    run_review(
        engine,
        object(),
        _btc_venue([]),
        "r1",
        lambda _s: dossier,
        dry_run=False,
        now=now,
        venue="demo",
    )
    assert seen[0]["flags"]["data_stale"] is stale


# --- J-N5: HALT means hands off -----------------------------------------------------------------


def _halt() -> Path:
    from argus.config import get_settings
    from argus.runs.watchdog import write_halt

    path = get_settings().halt_file
    write_halt(path, "operator: hands off")
    return path


def test_a_halted_system_asks_no_model_and_touches_nothing(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a kill-switch flatten the HALT file stands, and review still called the LLM and
    could `amend_stop` — which places a NEW algo when no stop exists."""
    _halt()
    asked = _capture(monkeypatch)
    venue = _btc_venue([_okx_algo("sl", "96000")])
    result = run_review(
        engine, object(), venue, "r1", _priced(99000.0), dry_run=False, venue="demo"
    )

    assert asked == [], "no model call under HALT"
    assert venue.did == []
    assert result.halted
    assert [o.action for o in result.outcomes] == ["HALTED"]
    assert not result.errors, "an operator's halt is not a failed run"


def test_a_halt_written_while_the_model_thinks_stops_the_action(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model call takes seconds to minutes; HALT is checked again just before the venue."""
    from argus.runs import review as module

    def slow(*_a: Any, **_k: Any) -> Any:
        _halt()
        return _Call(_decide("CLOSE"))

    monkeypatch.setattr(module, "ask_reviewer", slow)
    venue = _btc_venue([_okx_algo("sl", "96000")])
    result = run_review(
        engine, object(), venue, "r1", _priced(99000.0), dry_run=False, venue="demo"
    )

    assert venue.did == []
    assert result.outcomes[0].action == "CLOSE" and not result.outcomes[0].applied
    assert "HALT" in result.outcomes[0].detail


# --- J-N2: every review outcome on a live position is a ledger row ------------------------------


def _review_events(engine: Engine) -> list[dict[str, Any]]:
    import json

    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT symbol, plan_id, to_state, reason, detail FROM position_events"
                " WHERE machine = 'review' ORDER BY rowid"
            )
        ).mappings()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


class _ExplodingVenue(FakeVenue):
    def close(self, symbol: str) -> str:
        raise RuntimeError("51000 venue said no")


@pytest.mark.parametrize(
    ("answer", "venue_kind", "dry_run", "to_state", "status"),
    [
        ("CLOSE", "ok", False, "CLOSE", "applied"),
        ("CLOSE", "ok", True, "CLOSE", "dry_run"),
        ("HOLD", "ok", False, "HOLD", "held"),
        ("WIDEN", "ok", False, "MOVE_STOP", "refused"),
        ("CLOSE", "exploding", False, "CLOSE", "failed"),
        ("NO_MODEL", "ok", False, "UNREVIEWED", "unreviewed"),
    ],
)
def test_every_outcome_is_journalled(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    venue_kind: str,
    dry_run: bool,
    to_state: str,
    status: str,
) -> None:
    """CLOSE / TRIM / MOVE_STOP reached the venue and were only logged and printed: no row, so a
    post-mortem later called the system's own close "left the venue between ticks"."""
    from argus.journal import ledger

    if answer == "WIDEN":
        _patch(monkeypatch, _decide("MOVE_STOP", current_stop=90000.0, new_stop=95000.0))
    elif answer == "NO_MODEL":
        _patch(monkeypatch, AllModelsFailedError("every model failed"))
    else:
        _patch(monkeypatch, _decide(answer))
    cls = _ExplodingVenue if venue_kind == "exploding" else FakeVenue
    venue = cls(positions=[dict(BTC_POSITION)], algos=[_okx_algo("sl", "98000")])
    plan_id = _seed_plan(engine, symbol="BTC", payload=MANAGER_PAYLOAD)

    run_review(engine, object(), venue, "r1", _priced(99000.0), dry_run=dry_run, venue="demo")

    events = _review_events(engine)
    assert len(events) == 1
    event = events[0]
    assert event["symbol"] == "BTC/USDT:USDT"
    assert event["to_state"] == to_state
    assert event["detail"]["status"] == status
    assert event["detail"]["dry_run"] is dry_run
    if answer != "NO_MODEL":
        assert event["plan_id"] == plan_id
    assert ledger.verify(engine, "position_events") == (True, None)


def test_a_refused_stop_move_records_the_venue_level_not_the_model_s(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, _decide("MOVE_STOP", current_stop=90000.0, new_stop=95000.0))
    run_review(
        engine, object(), _btc_venue([_okx_algo("sl", "98000")]), "r1", _priced(1.0), venue="demo"
    )
    detail = _review_events(engine)[0]["detail"]
    assert detail["current_stop"] == 98000.0
    assert detail["new_stop"] == 95000.0


def test_halted_and_broken_positions_are_journalled_too(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    halt = _halt()
    run_review(
        engine, object(), _btc_venue([]), "r1", _priced(99000.0), dry_run=False, venue="demo"
    )
    assert [e["to_state"] for e in _review_events(engine)] == ["HALTED"]
    halt.unlink()

    def broken(_symbol: str) -> Any:
        raise RuntimeError("dossier exploded")

    run_review(engine, object(), _btc_venue([]), "r1", broken, dry_run=False, venue="demo")
    assert [e["to_state"] for e in _review_events(engine)] == ["HALTED", "UNREVIEWED"]


def test_a_journal_that_cannot_be_written_is_an_error(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An action nobody recorded is the failure J-N2 is about; it must not pass silently."""
    from argus.journal import ledger

    def refuse(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("database is locked")

    _patch(monkeypatch, _decide("CLOSE"))
    monkeypatch.setattr(ledger, "append", refuse)
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(
        engine, object(), venue, "r1", _priced(99000.0), dry_run=False, venue="demo"
    )

    assert venue.did == [("close", "BTC/USDT:USDT")], "the close itself still happened"
    assert any("journal" in e for e in result.errors)


# --- J-N3: a review that could not do its job says so, loudly -----------------------------------


def _alerts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from argus.observability import alerts

    sent: list[Any] = []

    def capture(batch: list[Any], notifier: Any = None) -> list[Any]:
        live = [a for a in batch if a is not None]
        sent.extend(live)
        return live

    monkeypatch.setattr(alerts, "dispatch", capture)
    return sent


def test_a_close_the_venue_refused_raises_a_critical_alert(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer says CLOSE, OKX rejects it, the position stays open — and the only trace was in
    journald. Exit 1 reached nobody: the unit has no OnFailure and no ping."""
    sent = _alerts(monkeypatch)
    _patch(monkeypatch, _decide("CLOSE"))
    venue = _ExplodingVenue(positions=[dict(BTC_POSITION)], algos=[_okx_algo("sl", "98000")])
    run_review(engine, object(), venue, "r1", _priced(99000.0), dry_run=False, venue="demo")

    assert [(a.key, a.severity) for a in sent] == [("review_apply_failed", "critical")]
    assert "BTC" in sent[0].message


def test_a_position_nobody_reviewed_raises_a_critical_alert(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _alerts(monkeypatch)

    def broken(_symbol: str) -> Any:
        raise RuntimeError("dossier exploded")

    run_review(engine, object(), _btc_venue([]), "r1", broken, dry_run=False, venue="demo")
    assert [(a.key, a.severity) for a in sent] == [("review_unreviewed", "critical")]


def test_an_unreadable_stop_list_raises_an_alert(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _alerts(monkeypatch)
    _patch(monkeypatch, _decide("HOLD"))
    run_review(engine, object(), _btc_venue(RuntimeError("429")), "r1", _priced(1.0), venue="demo")
    assert [a.key for a in sent] == ["review_stops_unknown"]


def test_a_venue_that_cannot_list_positions_raises_an_alert_and_the_error(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _alerts(monkeypatch)

    class _Down(FakeVenue):
        def fetch_positions(self) -> list[dict[str, Any]]:
            raise RuntimeError("50110 ip not whitelisted")

    with pytest.raises(RuntimeError):
        run_review(engine, object(), _Down(), "r1", _dossier, dry_run=False, venue="demo")
    assert [(a.key, a.severity) for a in sent] == [("review_failed", "critical")]


def test_a_clean_review_raises_nothing(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _alerts(monkeypatch)
    _patch(monkeypatch, _decide("CLOSE"))
    result = run_review(
        engine, object(), _btc_venue([_okx_algo("sl", "98000")]), "r1", _priced(1.0), venue="demo"
    )
    assert sent == [] and result.errors == []


def test_a_review_with_errors_is_a_failed_run_and_exits_non_zero(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run row said OK whatever happened; staleness checks and the dashboard read it."""
    from types import SimpleNamespace

    from sqlalchemy import text
    from typer.testing import CliRunner

    import argus.cli as cli
    from argus.exchange.fake import FakeExchange
    from argus.runs.review import ReviewResult

    def failing(
        engine: Any,
        router: Any,
        adapter: Any,
        run_id: str,
        dossier_for: Any,
        dry_run: bool,
        venue: str,
    ) -> Any:
        result = ReviewResult(run_id, dry_run, positions=1)
        result.errors.append("BTC/USDT:USDT: CLOSE failed: RuntimeError: 51000")
        return result

    venue = FakeExchange()
    settings = SimpleNamespace(mode="approve", is_live=True, venue="demo", argus_home=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr("argus.journal.db.open_journal", lambda *a, **k: engine)
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", lambda *a, **k: venue)
    monkeypatch.setattr("argus.llm.router.Router", lambda *a, **k: None)
    monkeypatch.setattr("argus.runs.review.run_review", failing)

    outcome = CliRunner().invoke(cli.app, ["review"])

    assert outcome.exit_code == 1, outcome.output
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, error FROM runs WHERE kind = 'review' AND run_id != 'r1'")
        ).one()
    assert row.status == "FAILED"
    assert "51000" in row.error


def test_under_halt_the_review_command_asks_no_model_for_post_mortems(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HALT means no model is asked (J-N5), but that held only inside `run_review`: the command
    went on to call the post-mortem LLM for every close it noticed. The close is still marked, as
    the watchdog marks it under HALT too: what is owed a post-mortem is read by plan, not from an
    unmarked close, so marking no longer loses one (F-151)."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import argus.cli as cli
    from argus.exchange.fake import FakeExchange
    from argus.journal import positions
    from argus.runs.review import ReviewResult
    from argus.runs.watchdog import write_halt

    asked: list[Any] = []
    halt_file = tmp_path / "HALT"
    write_halt(halt_file, "operator: hands off")
    positions.record(engine, "r1", [{"symbol": "SOL/USDT:USDT", "side": "long"}])

    def halted(engine: Any, router: Any, adapter: Any, run_id: str, *_a: Any, **_k: Any) -> Any:
        return ReviewResult(run_id, False, halted=True)

    venue = FakeExchange()
    settings = SimpleNamespace(
        mode="approve", is_live=True, venue="demo", argus_home=tmp_path, halt_file=halt_file
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr("argus.journal.db.open_journal", lambda *a, **k: engine)
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", lambda *a, **k: venue)
    monkeypatch.setattr("argus.llm.router.Router", lambda *a, **k: None)
    monkeypatch.setattr("argus.runs.review.run_review", halted)
    monkeypatch.setattr("argus.runs.review.write_postmortems", lambda *a, **k: asked.append(a))

    outcome = CliRunner().invoke(cli.app, ["review"])

    assert outcome.exit_code == 0, outcome.output
    assert asked == [], "no post-mortem model call under HALT"
    assert positions.newly_closed(engine, [], venue="demo") == []  # marked, as the watchdog would


# --- L-12: what the Reviewer's prompt carries to free providers and Logfire ---------------------


class _PromptRouter:
    """Stands in for the Router: records the prompt the real `reviewer.review` builds."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def call(self, **kwargs: Any) -> Any:
        self.prompts.append(kwargs["prompt"])
        return _Call(_decide("HOLD"))


def test_the_prompt_carries_no_raw_venue_row_and_no_exact_equity(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus.llm.golden import _dossier as golden_dossier
    from argus.llm.golden import _regime

    router = _PromptRouter()
    row = {**BTC_POSITION, "raw": {"uid": "77777777", "posId": "1234567890"}}
    venue = FakeVenue(positions=[row], algos=[_okx_algo("sl", "98000")])
    run_review(
        engine,
        router,  # type: ignore[arg-type]
        venue,
        "r1",
        lambda _s: golden_dossier("BTC", _regime("up"), technical={"price": 99000.0}),
        dry_run=False,
        portfolio={"equity_usd": 1234.56, "free_usd": 987.65, "open_risk_pct": 1.2},
        venue="demo",
    )

    prompt = router.prompts[0]
    assert "77777777" not in prompt and "1234567890" not in prompt
    assert "1234.56" not in prompt and "987.65" not in prompt
    assert "1200" in prompt, "the size of the account, to two figures, is still useful context"
    assert "1.2" in prompt, "risk percentages are not identifying and stay exact"


# --- L-14: a model that answers badly first, through the real router ---------------------------


def _model_router(engine: Engine, answers: list[dict[str, Any]]) -> Any:
    """A real Router over a scripted model: each request gets the next answer in `answers`."""
    import json

    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from argus.config import Settings
    from argus.exchange.breakers import BreakerRegistry
    from argus.llm.router import Router

    asked: list[int] = []

    def respond(_messages: Any, _info: AgentInfo) -> ModelResponse:
        asked.append(1)
        args = answers[min(len(asked), len(answers)) - 1]
        if not _info.output_tools:  # Groq leads the Reviewer and is asked for prompted JSON
            return ModelResponse(parts=[TextPart(json.dumps(args))])
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=args)])

    settings = Settings(
        _env_file=None,
        gemini_api_key="g",
        groq_api_key="g",
        mistral_api_key="g",
        cloudflare_api_token="g",
        cloudflare_account_id="a",
        openrouter_api_key="g",
        zhipu_api_key="g",
    )
    router = Router(
        engine=engine,
        settings=settings,
        breakers=BreakerRegistry(),
        model_factory=lambda _ref: FunctionModel(respond),
    )
    router.asked = asked  # type: ignore[attr-defined]
    return router


WIDEN = {
    "action": "MOVE_STOP",
    "side": "long",
    "rationale": "give it room",
    "current_stop": 98000.0,
    "new_stop": 95000.0,
}
TIGHTEN = {**WIDEN, "rationale": "lock in the move", "new_stop": 99000.0}


def _btc_dossier(_symbol: str) -> Any:
    from argus.llm.golden import _dossier as golden_dossier
    from argus.llm.golden import _regime

    return golden_dossier("BTC", _regime("up"), technical={"price": 100500.0})


def test_a_malformed_answer_then_a_valid_one_acts_on_the_valid_one(engine: Engine) -> None:
    """The schema rejects the widening; the model is asked again and tightens. Only the second
    answer may reach the venue."""
    router = _model_router(engine, [WIDEN, TIGHTEN])
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(engine, router, venue, "r1", _btc_dossier, dry_run=False, venue="demo")

    assert len(router.asked) == 2
    assert venue.did == [("amend", 99000.0)]
    assert result.outcomes[0].applied and result.errors == []


def test_a_model_that_only_ever_widens_leaves_the_position_alone(engine: Engine) -> None:
    router = _model_router(engine, [WIDEN])
    venue = _btc_venue([_okx_algo("sl", "98000")])
    result = run_review(engine, router, venue, "r1", _btc_dossier, dry_run=False, venue="demo")

    assert venue.did == []
    assert result.outcomes[0].action == "UNREVIEWED"
    assert result.errors


# --- R-1 (runs view): the dossier is built for the asset, not for a venue spelling --------------


@pytest.mark.parametrize("spelling", ["BTC/USDT:USDT", "BTC-USDT-SWAP", "BTC"])
def test_the_dossier_is_asked_for_the_asset_whatever_the_venue_calls_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """`cli.review`'s builder keys the listed universe by asset (`BTC`) and took
    `symbol.split("-")[0]` — which leaves a ccxt-spelled `BTC/USDT:USDT` whole, builds a bogus
    `Asset`, and every real position errored out unreviewed."""
    asked: list[str] = []

    def dossier_for(symbol: str) -> Any:
        asked.append(symbol)
        return _priced(99000.0)(symbol)

    _patch(monkeypatch, _decide("HOLD"))
    venue = FakeVenue(positions=[{**BTC_POSITION, "symbol": spelling}], algos=[])
    run_review(engine, object(), venue, "r1", dossier_for, dry_run=False, venue="demo")
    assert asked == ["BTC"]


# --- F-151: a close the watchdog noticed first still gets its post-mortem -----------------------

SOL_SWAP = "SOL-USDT-SWAP"


def _sound(*_a: Any, **_k: Any) -> Any:
    from argus.llm.schemas import PostMortem

    return _Call(
        PostMortem(
            process_verdict="sound",
            would_take_again=True,
            entry_timing="good",
            sizing="right",
            stop_placement="right",
            missed_signals=[],
            do_differently="",
            feedback="The thesis held; price reached the stop first.",
        )
    )


def _watchdog_sees_the_stop_fill(engine: Engine, tmp_path: Path) -> tuple[str, Any]:
    """A filled SOL trade, seen open by one real watchdog tick and gone at the next: the stop
    filled in between, and the second tick marks the position CLOSED. Returns the plan that
    opened it and the venue."""
    from argus.config import Settings
    from argus.exchange.fake import FakeExchange
    from argus.journal.db import start_run
    from argus.notify.telegram import Notifier, RecordingBot
    from argus.runs.watchdog import Watchdog

    placed = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    plan_id = _seed_plan(engine, order=("OPEN", "FILLED"), at=placed)
    venue = FakeExchange()
    venue.open_unprotected(SOL_SWAP)
    venue.place_stop(SOL_SWAP, Decimal("96"))
    wd = Watchdog(
        exchange=venue,
        engine=engine,
        notifier=Notifier(bot=RecordingBot(), chat_id="42"),
        settings=Settings(mode="demo", argus_home=tmp_path),
    )
    with start_run(engine, "watchdog", "demo") as run_id:
        wd.run_once(run_id)
    venue.close(SOL_SWAP)
    with start_run(engine, "watchdog", "demo") as run_id:
        wd.run_once(run_id)
    return plan_id, venue


def _scheduled_review(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    venue: Any,
    asked: list[str],
    *flags: str,
) -> Any:
    """`argus review` as the timer runs it, armed, with the Reviewer itself stubbed out: the
    post-mortem pass after it is what is under test. `asked` records every post-mortem call."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import argus.cli as cli
    from argus.runs.review import ReviewResult

    armed = SimpleNamespace(
        mode="approve",
        is_live=True,
        venue="demo",
        argus_home=tmp_path,
        halt_file=tmp_path / "HALT",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: armed)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr("argus.journal.db.open_journal", lambda *a, **k: engine)
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", lambda *a, **k: venue)
    monkeypatch.setattr("argus.llm.router.Router", lambda *a, **k: None)
    monkeypatch.setattr(
        "argus.runs.review.run_review",
        lambda engine, router, adapter, run_id, dossier_for, dry_run, **_k: ReviewResult(
            run_id, dry_run
        ),
    )
    monkeypatch.setattr(
        "argus.llm.agents.postmortem.analyse",
        lambda *a, **k: asked.append(a[2]["thesis"]) or _sound(),
    )
    result = CliRunner().invoke(cli.app, ["review", *flags])
    assert result.exit_code == 0, result.output
    return result


def _judged(engine: Engine) -> list[str]:
    from sqlalchemy import text

    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT plan_id FROM postmortems"))]


def test_a_close_the_watchdog_noticed_first_still_gets_its_post_mortem(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog ticks every 20 minutes and the review four times a day, so the watchdog nearly
    always notices a close first and marks it CLOSED. The review then asked `newly_closed`, got
    nothing back, and the trade never had a post-mortem — only a close inside the gap between
    the last tick and a review was ever judged (F-151)."""
    plan_id, venue = _watchdog_sees_the_stop_fill(engine, tmp_path)
    asked: list[str] = []

    _scheduled_review(engine, tmp_path, monkeypatch, venue, asked)
    _scheduled_review(engine, tmp_path, monkeypatch, venue, asked)

    assert _judged(engine) == [plan_id]
    assert asked == ["EMA reclaim"], "judged once, not again by every later review"


def test_a_post_mortem_held_back_by_halt_is_written_once_it_clears(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HALT means no model is asked (J-N5) — but the watchdog keeps marking closes under HALT, so
    a post-mortem that waited on an unmarked close waited for ever."""
    from argus.runs.watchdog import clear_halt, write_halt

    plan_id, venue = _watchdog_sees_the_stop_fill(engine, tmp_path)
    asked: list[str] = []

    write_halt(tmp_path / "HALT", "operator: hands off")
    _scheduled_review(engine, tmp_path, monkeypatch, venue, asked)
    assert asked == [] and _judged(engine) == []

    clear_halt(tmp_path / "HALT")
    _scheduled_review(engine, tmp_path, monkeypatch, venue, asked)
    assert _judged(engine) == [plan_id]


def test_a_dry_run_judges_the_close_it_noticed_itself_and_stores_nothing(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run marks nothing, so a close it noticed before the watchdog did is not in the
    journal yet — it is judged from what the run saw, and is still owed a stored verdict."""
    from argus.journal import positions

    _seed_plan(engine, order=("OPEN", "FILLED"), at="2026-10-01T10:00:00+00:00")
    _snapshot(engine, "2026-10-01T12:00:00+00:00")
    asked: list[str] = []

    _scheduled_review(engine, tmp_path, monkeypatch, FakeVenue(positions=[]), asked, "--dry-run")

    assert asked == ["EMA reclaim"]
    assert _judged(engine) == []
    assert [c.symbol for c in positions.newly_closed(engine, [], venue="demo")] == ["SOL/USDT:USDT"]


def test_a_close_older_than_the_lookback_is_no_longer_due(engine: Engine) -> None:
    """Bounded, so a close no model will judge is not re-read by every review for ever."""
    from argus.runs.review import POSTMORTEM_LOOKBACK, postmortems_due

    _seed_plan(engine, order=("OPEN", "FILLED"), at="2026-10-01T10:00:00+00:00")
    _snapshot(engine, "2026-10-01T12:00:00+00:00")
    _closes(engine, "2026-10-01T16:00:00+00:00")
    closed_at = datetime.fromisoformat("2026-10-01T16:00:00+00:00")

    inside = postmortems_due(engine, closed_at + POSTMORTEM_LOOKBACK, venue="demo")
    assert [c.symbol for c in inside] == ["SOL/USDT:USDT"]
    assert (
        postmortems_due(
            engine, closed_at + POSTMORTEM_LOOKBACK + timedelta(seconds=1), venue="demo"
        )
        == []
    )


def test_a_close_marked_by_two_runs_at_once_is_judged_once(engine: Engine) -> None:
    """The review's post-mortem pass runs outside the run lock, so it can mark a close in the same
    moment the watchdog does: two CLOSED rows for one trade, and two model calls for one plan."""
    from argus.journal import positions
    from argus.runs.review import postmortems_due

    _seed_plan(engine, order=("OPEN", "FILLED"), at="2026-10-01T10:00:00+00:00")
    _snapshot(engine, "2026-10-01T12:00:00+00:00")
    closed = positions.newly_closed(engine, [], venue="demo")
    for at in ("2026-10-01T16:00:00+00:00", "2026-10-01T16:00:01+00:00"):
        positions.mark_closed(engine, "r1", closed, now=datetime.fromisoformat(at))

    due = postmortems_due(engine, datetime.fromisoformat("2026-10-02T00:00:00+00:00"), venue="demo")
    assert [c.symbol for c in due] == ["SOL/USDT:USDT"]


def test_an_outage_costs_one_failed_post_mortem_per_review_not_one_per_trade(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each due trade got its own router chain — up to four models at 45s each — so a backlog met
    during an outage could outrun the review unit's 900s limit. Newest first, so a trade no model
    can ever judge holds up only the ones older than it."""
    from argus.runs import review as module

    for day, thesis in (("01", "first"), ("02", "second")):
        payload = {"entry": [99.8, 100.2], "stop": 96.0, "thesis": thesis, "atr_14": 4.0}
        _seed_plan(
            engine, order=("OPEN", "FILLED"), at=f"2026-10-{day}T10:00:00+00:00", payload=payload
        )
        _snapshot(engine, f"2026-10-{day}T12:00:00+00:00")
        _closes(engine, f"2026-10-{day}T16:00:00+00:00")
    asked: list[str] = []

    def down(_router: Any, _run_id: str, plan: dict[str, Any], _outcome: Any) -> Any:
        asked.append(plan["thesis"])
        raise module.AllModelsFailedError("every provider down")

    monkeypatch.setattr("argus.llm.agents.postmortem.analyse", down)
    now = datetime.fromisoformat("2026-10-03T00:00:00+00:00")

    assert (
        module.write_postmortems(
            engine,
            object(),
            "r1",
            module.postmortems_due(engine, now, venue="demo"),
            venue="demo",
        )
        == []
    )
    assert asked == ["second"]
    assert len(module.postmortems_due(engine, now, venue="demo")) == 2, (
        "both still due at the next review"
    )


def test_a_close_on_the_other_account_is_never_due_for_a_post_mortem(engine: Engine) -> None:
    """One journal holds both accounts (F-149, F-150). `closed_since` came from a branch written
    before that scoping, so a demo review would have judged a live close — a verdict about the
    wrong account, fed to the Reflector as if it were this one's."""
    from sqlalchemy import text

    from argus.journal import positions
    from argus.journal.positions import closed_since

    with engine.begin() as conn:
        for run_id, venue in (("live-run", "live"), ("demo-run", "demo")):
            conn.execute(
                text(
                    "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                    " VALUES (:r,'watchdog','approve',:t,'OK',:v)"
                ),
                {"r": run_id, "t": "2026-10-01T00:00:00+00:00", "v": venue},
            )
    positions.record(engine, "live-run", [{"symbol": "BTC/USDT:USDT", "side": "long"}])
    closed = positions.newly_closed(engine, [], venue="live")
    positions.mark_closed(engine, "live-run", closed)
    since = datetime.fromisoformat("2026-09-01T00:00:00+00:00")

    assert [c.symbol for c in closed_since(engine, since, venue="live")] == ["BTC/USDT:USDT"]
    assert closed_since(engine, since, venue="demo") == []


def test_an_explicit_halt_file_still_validates_settings(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P0-5: a direct caller passing halt_file must not skip the Settings (MODE) validator."""
    from argus.runs import review

    def refuse() -> Any:
        raise ValueError("MODE=auto requires LIVE_TRADING")

    monkeypatch.setattr(review, "get_settings", refuse)
    with pytest.raises(ValueError, match="LIVE_TRADING"):
        run_review(
            engine, object(), FakeVenue(), "r1", _dossier, True,
            halt_file=tmp_path / "HALT", venue="demo",
        )  # fmt: skip
