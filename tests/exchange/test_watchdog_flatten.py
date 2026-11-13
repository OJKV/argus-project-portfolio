"""Review 2026-10-07 X-N4: the kill-switch flatten must cancel resting entries on flat symbols.

`_flatten` walked the open *positions* and cancelled orders only on their symbols. A SOL limit
entry resting on a symbol with no position survived the HALT and could fill an hour later —
opening a brand-new position after the drawdown kill-switch had fired.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.fake import FakeExchange, FakeOrder
from argus.journal.db import open_journal
from argus.notify.telegram import RecordingBot
from argus.runs.watchdog import KILL_MARKER, write_halt
from tests.runs.test_watchdog import _armed, _run_id

SOL = "SOL-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(mode="demo", argus_home=tmp_path)


def _resting_sol_entry(ex: FakeExchange) -> FakeOrder:
    order = FakeOrder(
        order_id="o-sol",
        cl_ord_id="solentry",
        symbol=SOL,
        side="buy",
        kind="entry",
        order_type="limit",
        size=Decimal("1"),
        price=Decimal("90"),
    )
    ex.orders[order.order_id] = order
    return order


def test_kill_switch_cancels_an_entry_resting_on_a_symbol_with_no_position(
    engine: Engine, settings: Settings
) -> None:
    ex = FakeExchange(equity=Decimal("1000"))
    wd = _armed(ex, engine, settings, RecordingBot())
    order = _resting_sol_entry(ex)

    result = wd.run_once(_run_id(engine))

    assert result.killed
    assert order.state == "CANCELLED", "a resting entry on a flat symbol survived the HALT"


def test_the_halted_retry_also_cancels_entries_on_flat_symbols(
    engine: Engine, settings: Settings
) -> None:
    """A kill-switch HALT already on disk: the retry tick must sweep the whole book too."""
    ex = FakeExchange(equity=Decimal("1000"))
    ex.open_unprotected("BTC-USDT-SWAP")
    order = _resting_sol_entry(ex)
    write_halt(settings.halt_file, f"{KILL_MARKER} drawdown 20.0% from 30d high")
    from tests.runs.test_watchdog import _watchdog

    _watchdog(ex, engine, settings, RecordingBot()).run_once(_run_id(engine))

    assert order.state == "CANCELLED"


def test_a_halted_retry_with_no_positions_left_still_cancels_a_surviving_entry(
    engine: Engine, settings: Settings
) -> None:
    """Every position closed but a cancel failed last tick: the entry must not outlive HALT."""
    ex = FakeExchange(equity=Decimal("1000"))
    order = _resting_sol_entry(ex)
    write_halt(settings.halt_file, f"{KILL_MARKER} drawdown 20.0% from 30d high")
    from tests.runs.test_watchdog import _watchdog

    _watchdog(ex, engine, settings, RecordingBot()).run_once(_run_id(engine))

    assert order.state == "CANCELLED"
