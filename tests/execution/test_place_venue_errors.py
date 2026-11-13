"""What `place_approved` does when the venue call raises.

Review 2026-09-18 S-3: the exception text — ccxt's whole OKX response body, which on a 50110
names the VPS's IP address — went verbatim into the hash-chained REJECTED reason and the raised
error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.execution.place import PlacementError, place_approved
from argus.journal.db import open_journal
from tests.execution.test_place import FakeVenue, _gate, _plan, _seed

IP4 = "203.0.113.7"
OKX_50110 = '{"code":"50110","msg":"Your IP ' + IP4 + ' is not included in the whitelist."}'


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = open_journal(tmp_path / "journal.db")
    _seed(eng, "run-1", "plan-1")
    return eng


def _reasons(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [str(r) for r in conn.execute(text("SELECT reason FROM position_events")).scalars()]


def test_a_refusal_naming_the_vps_address_is_stored_without_it(engine: Engine) -> None:
    venue = FakeVenue(fail=RuntimeError(OKX_50110))
    with pytest.raises(PlacementError) as raised:
        place_approved(engine, venue, "run-1", _plan(), _gate(), "plan-1", "SOL-USDT-SWAP")
    assert IP4 not in str(raised.value)
    stored = " ".join(_reasons(engine))
    assert "50110" in stored
    assert IP4 not in stored
