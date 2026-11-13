"""Review 2026-10-07 X-N5: a placement timeout journalled REJECTED for an order that was live.

`place_approved` turned *any* exception — `ccxt.RequestTimeout` included — into REJECTED without
asking the venue. A timeout on a market entry whose reply was lost left a live position the
journal called rejected: nothing would amend, cancel or reconcile it, and a second approval would
double the size.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ccxt
import pytest
from sqlalchemy import Engine, text

from argus.exchange.fake import FakeExchange
from argus.execution.lifecycle import order_state
from argus.execution.place import PlacementError, place_approved
from argus.journal.db import open_journal
from tests.execution.test_place import _gate, _plan, _seed

SOL = "SOL-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_journal(tmp_path / "journal.db")
    _seed(eng, "run-1", "plan-1")
    return eng


def _only_order(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM orders")).mappings().all()
    assert len(rows) == 1
    return dict(rows[0])


def test_a_timeout_the_venue_accepted_is_journalled_open_with_its_id(engine: Engine) -> None:
    venue = FakeExchange(accept_then_fail=ccxt.RequestTimeout("okx POST /trade/order timed out"))
    order_id = place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", SOL)

    assert order_id is not None
    assert order_state(engine, order_id) == "OPEN"
    held = next(iter(venue.orders.values()))
    assert _only_order(engine)["exchange_id"] == held.order_id


def test_a_timeout_the_venue_never_saw_is_rejected(engine: Engine) -> None:
    venue = FakeExchange()
    venue.fail_next = ccxt.RequestTimeout("connect timed out")
    with pytest.raises(PlacementError):
        place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", SOL)
    assert order_state(engine, _only_order(engine)["order_id"]) == "REJECTED"


def test_a_timeout_that_cannot_be_checked_is_left_unknown_not_rejected(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW is "sent, outcome unknown": the resting sweep and the orphan check settle it later.

    REJECTED would be a claim we cannot back, and a REJECTED row is never looked at again.
    """
    venue = FakeExchange(accept_then_fail=ccxt.RequestTimeout("timed out"))

    def down(symbol: str, cl_ord_id: str) -> dict[str, Any] | None:
        raise ccxt.NetworkError("venue unreachable")

    monkeypatch.setattr(venue, "fetch_order_by_client_id", down)
    with pytest.raises(PlacementError, match="unknown"):
        place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", SOL)
    assert order_state(engine, _only_order(engine)["order_id"]) == "NEW"


def test_a_definite_refusal_is_still_rejected(engine: Engine) -> None:
    venue = FakeExchange()
    venue.fail_next = ccxt.InsufficientFunds('okx {"code":"51008"}')
    with pytest.raises(PlacementError, match="51008"):
        place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", SOL)
    assert order_state(engine, _only_order(engine)["order_id"]) == "REJECTED"
