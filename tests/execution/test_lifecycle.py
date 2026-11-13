"""An order's life, recorded and constrained.

Every state change goes through the state machine, so a bug that would put an order in an
impossible state raises instead of writing a row nobody can explain later. The ledger is
insert-only, so the history of a trade is reconstructible even when the last state is wrong.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.execution.lifecycle import (
    IllegalTransitionError,
    apply_fill,
    order_state,
    record_order,
    transition_order,
)
from argus.journal.db import open_journal


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_journal(tmp_path / "journal.db")
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES ('r1','manager','demo','2026-09-20T00:00:00+00:00','RUNNING',1,'-')"
            )
        )
    return eng


ORDER = {
    "symbol": "BTC-USDT-SWAP",
    "side": "buy",
    "size": Decimal("1.1"),
    "order_type": "limit",
    "price": Decimal("104100"),
    "cl_ord_id": "argusabc123",
    "attach_algos": [{"kind": "sl", "trigger_px": Decimal("101500")}],
}


def test_a_recorded_order_starts_as_new(engine: Engine) -> None:
    order_id = record_order(engine, "r1", ORDER)
    assert order_state(engine, order_id) == "NEW"


def test_the_client_order_id_is_unique(engine: Engine) -> None:
    """The database enforces what idempotency promises: the same intention cannot be two rows."""
    from sqlalchemy.exc import IntegrityError

    record_order(engine, "r1", ORDER)
    with pytest.raises(IntegrityError):
        record_order(engine, "r1", ORDER)


def test_a_legal_transition_is_recorded(engine: Engine) -> None:
    order_id = record_order(engine, "r1", ORDER)
    transition_order(engine, order_id, "OPEN", reason="acked by venue")
    assert order_state(engine, order_id) == "OPEN"


def test_an_impossible_transition_raises_instead_of_writing(engine: Engine) -> None:
    """NEW to FILLED skips acknowledgement. It is the kind of jump a retry bug makes, and the
    row it would leave is one nobody can explain a month later."""
    order_id = record_order(engine, "r1", ORDER)
    with pytest.raises(IllegalTransitionError):
        transition_order(engine, order_id, "FILLED")
    assert order_state(engine, order_id) == "NEW"


def test_a_terminal_order_cannot_be_revived(engine: Engine) -> None:
    order_id = record_order(engine, "r1", ORDER)
    transition_order(engine, order_id, "OPEN")
    transition_order(engine, order_id, "CANCELLED")
    with pytest.raises(IllegalTransitionError):
        transition_order(engine, order_id, "OPEN")


def test_a_partial_fill_leaves_the_order_partial(engine: Engine) -> None:
    order_id = record_order(engine, "r1", ORDER)
    transition_order(engine, order_id, "OPEN")
    apply_fill(
        engine,
        "r1",
        order_id,
        price=Decimal("104100"),
        size=Decimal("0.5"),
        fee=Decimal("-0.05"),
        fee_ccy="USDT",
    )
    assert order_state(engine, order_id) == "PARTIAL"


def test_fills_that_complete_the_size_close_the_order(engine: Engine) -> None:
    order_id = record_order(engine, "r1", ORDER)
    transition_order(engine, order_id, "OPEN")
    apply_fill(
        engine,
        "r1",
        order_id,
        price=Decimal("104100"),
        size=Decimal("0.5"),
        fee=Decimal("-0.05"),
        fee_ccy="USDT",
    )
    apply_fill(
        engine,
        "r1",
        order_id,
        price=Decimal("104150"),
        size=Decimal("0.6"),
        fee=Decimal("-0.06"),
        fee_ccy="USDT",
    )
    assert order_state(engine, order_id) == "FILLED"


def test_an_overfill_is_refused(engine: Engine) -> None:
    """More filled than ordered means we are reading someone else's fill, or reading ours twice."""
    order_id = record_order(engine, "r1", ORDER)
    transition_order(engine, order_id, "OPEN")
    with pytest.raises(ValueError, match="exceeds"):
        apply_fill(
            engine,
            "r1",
            order_id,
            price=Decimal("104100"),
            size=Decimal("2.0"),
            fee=Decimal("-0.2"),
            fee_ccy="USDT",
        )


def test_money_is_stored_as_text_not_float(engine: Engine) -> None:
    order_id = record_order(engine, "r1", ORDER)
    apply_fill(
        engine,
        "r1",
        order_id,
        price=Decimal("104100.12345678"),
        size=Decimal("0.1"),
        fee=Decimal("-0.01"),
        fee_ccy="USDT",
    )
    with engine.connect() as conn:
        price = conn.execute(text("SELECT price FROM fills")).scalar()
    assert price == "104100.12345678"


def test_a_fill_on_a_new_order_walks_through_open_first(engine: Engine) -> None:
    """Muse P0-3: a fill before the OPEN step used to leave the order NEW with fills on record."""
    order_id = record_order(engine, "r1", ORDER)
    apply_fill(engine, "r1", order_id, Decimal("104100"), Decimal("1.1"), Decimal("0.5"), "USDT")
    assert order_state(engine, order_id) == "FILLED"


def test_a_partial_fill_on_a_new_order_lands_in_partial(engine: Engine) -> None:
    order_id = record_order(engine, "r1", ORDER)
    apply_fill(engine, "r1", order_id, Decimal("104100"), Decimal("0.4"), Decimal("0.1"), "USDT")
    assert order_state(engine, order_id) == "PARTIAL"


def test_a_fill_on_a_terminal_order_logs_an_error_not_silence(engine: Engine) -> None:
    from structlog.testing import capture_logs

    order_id = record_order(engine, "r1", ORDER)
    transition_order(engine, order_id, "REJECTED")
    with capture_logs() as logs:
        apply_fill(engine, "r1", order_id, Decimal("1"), Decimal("0.1"), Decimal("0"), "USDT")
    assert any(e["log_level"] == "error" and "skipped" in e["event"] for e in logs)
