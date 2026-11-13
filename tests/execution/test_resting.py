"""The life of a resting entry order, ended deliberately (F-143, F-144).

Before this, nothing cancelled an unfilled entry — `entry_valid_hours` and
`entry_drift_cancel_pct` were read by no code — and nothing told the journal when an order left the
venue, so the dashboard showed three "open" orders that did not exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import (
    order_state,
    record_order,
    record_venue_ids,
    transition_order,
)
from argus.execution.resting import sweep
from argus.journal.db import open_journal

SYMBOL = "BTC-USDT-SWAP"
ENTRY = Decimal("75000")


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_journal(tmp_path / "journal.db")
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES ('r1','manager','demo','2026-10-06T00:00:00+00:00','OK',0,'demo')"
            )
        )
    return eng


def _resting(
    engine: Engine,
    venue: FakeExchange,
    *,
    cl: str = "argus1",
    side: str = "buy",
    legacy: bool = False,
) -> str:
    """An entry the journal believes is OPEN and the fake venue actually holds."""
    stop = ENTRY * Decimal("0.97") if side == "buy" else ENTRY * Decimal("1.03")
    order = {
        "symbol": SYMBOL,
        "side": side,
        "size": Decimal("1"),
        "order_type": "limit",
        "price": ENTRY,
        "cl_ord_id": cl,
        "attach_algos": [{"kind": "sl", "trigger_px": stop}],
    }
    order_id = record_order(engine, "r1", order)
    ids = venue.place_bracket(order)
    if not legacy:
        record_venue_ids(engine, order_id, ids)
    transition_order(engine, order_id, "OPEN")
    return order_id


def _venue_id(venue: FakeExchange) -> str:
    return next(iter(venue.orders))


def _hours(n: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=n)


def test_an_entry_older_than_its_validity_is_cancelled_at_the_venue(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)

    swept = sweep(engine, venue, now=_hours(13), valid_hours=12, drift_pct=2.0, venue="demo")

    assert venue.orders[_venue_id(venue)].state == "CANCELLED"
    assert order_state(engine, order_id) == "EXPIRED"
    assert [s.to_state for s in swept.settled] == ["EXPIRED"]


def test_a_young_entry_at_its_price_is_left_alone(engine: Engine) -> None:
    """The counterpart that keeps the sweep from cancelling every order it sees."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)

    assert (
        sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo").settled
        == []
    )
    assert venue.orders[_venue_id(venue)].state == "OPEN"
    assert order_state(engine, order_id) == "OPEN"


def test_a_buy_the_market_ran_away_from_is_cancelled(engine: Engine) -> None:
    """Price 3% above a buy limit: the setup it was planned for has gone."""
    venue = FakeExchange(marks={SYMBOL: ENTRY * Decimal("1.03")})
    order_id = _resting(engine, venue)

    swept = sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")

    assert venue.orders[_venue_id(venue)].state == "CANCELLED"
    assert order_state(engine, order_id) == "CANCELLED"
    assert "drift" in swept.settled[0].reason


def test_price_falling_towards_a_buy_is_not_drift(engine: Engine) -> None:
    """Moving TOWARDS the limit is the order working, not the thesis dying."""
    venue = FakeExchange(marks={SYMBOL: ENTRY * Decimal("0.97")})
    _resting(engine, venue)
    assert (
        sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo").settled
        == []
    )


def test_a_sell_the_market_ran_away_from_is_cancelled(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY * Decimal("0.97")})
    order_id = _resting(engine, venue, side="sell")
    sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")
    assert order_state(engine, order_id) == "CANCELLED"


def test_an_order_gone_from_the_venue_is_closed_in_the_journal(engine: Engine) -> None:
    """F-144: cancelled elsewhere, nothing to cancel — but the journal must stop calling it open."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)
    venue.orders[_venue_id(venue)].state = "CANCELLED"

    sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")

    assert order_state(engine, order_id) == "CANCELLED"


def test_an_order_that_filled_is_recorded_as_filled_not_cancelled(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)
    venue.fill(_venue_id(venue))

    sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")

    assert order_state(engine, order_id) == "FILLED"


def test_a_dry_run_cancels_nothing_and_writes_nothing(engine: Engine) -> None:
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)

    swept = sweep(
        engine, venue, now=_hours(13), valid_hours=12, drift_pct=2.0, dry_run=True, venue="demo"
    )

    assert [s.to_state for s in swept.settled] == ["EXPIRED"]
    assert venue.orders[_venue_id(venue)].state == "OPEN"
    assert order_state(engine, order_id) == "OPEN"


def test_only_the_expired_order_is_cancelled_never_its_neighbours(engine: Engine) -> None:
    """Cancel by id. `cancel_all(symbol)` would take every resting order on the instrument."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    old = _resting(engine, venue, cl="argus-old")
    old_venue_id = _venue_id(venue)
    with engine.begin() as conn:
        # Ageing a row is an edit of history, which the ledger guard refuses (J-N1).
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS orders_write_once")
        conn.execute(
            text("UPDATE orders SET created_at = :t WHERE order_id = :o"),
            {"t": (datetime.now(UTC) - timedelta(hours=20)).isoformat(), "o": old},
        )
    young = _resting(engine, venue, cl="argus-young")

    sweep(engine, venue, now=datetime.now(UTC), valid_hours=12, drift_pct=2.0, venue="demo")

    assert venue.orders[old_venue_id].state == "CANCELLED"
    assert order_state(engine, old) == "EXPIRED"
    assert order_state(engine, young) == "OPEN"
    assert sum(o.state == "OPEN" for o in venue.orders.values()) == 1


def test_the_scheduled_review_runs_the_sweep(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, not the logic: a unit-tested sweep nobody calls is F-143 over again."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import argus.cli as cli
    from argus.runs.review import ReviewResult

    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)
    with engine.begin() as conn:
        # Ageing a row is an edit of history, which the ledger guard refuses (J-N1).
        conn.exec_driver_sql("DROP TRIGGER IF EXISTS orders_write_once")
        conn.execute(
            text("UPDATE orders SET created_at = :t WHERE order_id = :o"),
            {"t": (datetime.now(UTC) - timedelta(hours=30)).isoformat(), "o": order_id},
        )

    armed = SimpleNamespace(mode="approve", is_live=True, venue="demo", argus_home=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: armed)
    monkeypatch.setattr(cli, "_telemetry", lambda *a, **k: None)
    monkeypatch.setattr("argus.journal.db.open_journal", lambda *a, **k: engine)
    monkeypatch.setattr("argus.exchange.okx.OKXAdapter", lambda *a, **k: venue)
    monkeypatch.setattr("argus.llm.router.Router", lambda *a, **k: None)
    monkeypatch.setattr(
        "argus.runs.review.run_review",
        lambda engine, router, adapter, run_id, dossier_for, dry_run, venue: ReviewResult(
            run_id, dry_run
        ),
    )

    result = CliRunner().invoke(cli.app, ["review"])

    assert result.exit_code == 0, result.output
    assert order_state(engine, order_id) == "EXPIRED"
    assert venue.orders[_venue_id(venue)].state == "CANCELLED"


def test_an_entry_that_filled_and_stopped_out_is_filled_not_cancelled(engine: Engine) -> None:
    """Review finding: with no position left, inferring "gone" called a real trade cancelled.

    The venue is asked instead, and it says the order filled.
    """
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)
    venue.fill(_venue_id(venue))
    venue.positions.clear()  # the stop hit before the review looked

    sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")

    assert order_state(engine, order_id) == "FILLED"


def test_one_failing_order_does_not_strand_the_rest(engine: Engine) -> None:
    """Review finding: one exception used to end the sweep, leaving later stale entries live."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    first = _resting(engine, venue, cl="argus-a")
    first_venue_id = _venue_id(venue)
    second = _resting(engine, venue, cl="argus-b")
    real_cancel = venue.cancel_order

    def flaky(symbol: str, order_id: str) -> None:
        if order_id == first_venue_id:
            raise ConnectionError("venue hiccup")
        real_cancel(symbol, order_id)

    venue.cancel_order = flaky  # type: ignore[method-assign]
    result = sweep(engine, venue, now=_hours(13), valid_hours=12, drift_pct=2.0, venue="demo")

    assert order_state(engine, first) == "OPEN"
    assert len(result.errors) == 1 and "venue hiccup" in result.errors[0]
    assert order_state(engine, second) == "EXPIRED"


def test_a_legacy_row_is_matched_by_its_client_order_id(engine: Engine) -> None:
    """Rows from before venue ids were stored still expire, matched on our own cl_ord_id."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue, legacy=True)

    sweep(engine, venue, now=_hours(13), valid_hours=12, drift_pct=2.0, venue="demo")

    assert venue.orders[_venue_id(venue)].state == "CANCELLED"
    assert order_state(engine, order_id) == "EXPIRED"


def test_a_legacy_row_is_asked_about_by_its_client_id_while_a_position_is_open(
    engine: Engine,
) -> None:
    """No venue id, and an open position that may or may not be this order. This used to be
    reported for a human on every pass; the venue answers by client id (review 2026-10-07)."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue, legacy=True)
    venue.fill(_venue_id(venue))

    result = sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")

    assert result.errors == []
    assert order_state(engine, order_id) == "FILLED"
    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT exchange_id FROM orders WHERE order_id = :o"), {"o": order_id}
        ).scalar_one()
    assert stored == _venue_id(venue)


def test_an_order_the_venue_has_purged_is_closed_as_cancelled(engine: Engine) -> None:
    """Found on the real venue (dry run, 2026-10-06): OKX answers 51603 "Order does not exist"
    for an unfilled order cancelled more than two hours ago, because it purges them. Filled orders
    are retained, so off the book and off the record means it died unfilled."""
    venue = FakeExchange(marks={SYMBOL: ENTRY})
    order_id = _resting(engine, venue)
    venue.orders.clear()

    result = sweep(engine, venue, now=_hours(1), valid_hours=12, drift_pct=2.0, venue="demo")

    assert result.errors == []
    assert order_state(engine, order_id) == "CANCELLED"
