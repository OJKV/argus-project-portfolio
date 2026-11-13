"""Review 2026-10-07 X-N6 and 2026-09-18 X-11: one failing step must not abort the safety pass.

The watchdog's steps ran unguarded after reconcile. A "database is locked" on the ledger write
that follows a *successful* `amend_stop` escaped the repair, so the stop was live at the venue
while the pass died — no healthcheck ping, no alert dispatch, and the remaining positions never
looked at. A balance error did the same from the drawdown step.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from argus.config import Settings
from argus.exchange.fake import FakeExchange
from argus.journal import ledger
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs import watchdog as watchdog_module
from argus.runs.watchdog import Watchdog

BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"
HC = "https://hc-ping.example/uuid"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.fixture
def pings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(watchdog_module.httpx, "get", lambda url, **_k: sent.append(url))
    return sent


def _wd(ex: FakeExchange, engine: Engine, tmp_path: Path, bot: RecordingBot) -> Watchdog:
    return Watchdog(
        exchange=ex,
        engine=engine,
        notifier=Notifier(bot=bot, chat_id="42"),
        settings=Settings(mode="demo", argus_home=tmp_path, healthchecks_url=HC),
    )


def _tick(wd: Watchdog, engine: Engine) -> Any:
    with start_run(engine, "watchdog", "demo") as run_id:
        return wd.run_once(run_id)


def test_a_locked_journal_after_a_successful_repair_still_counts_as_repaired(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pings: list[str]
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    real_append = ledger.append

    def locked(eng: Engine, table: str, payload: dict[str, Any]) -> str:
        if table == "position_events":
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return real_append(eng, table, payload)

    monkeypatch.setattr(watchdog_module.ledger, "append", locked)
    bot = RecordingBot()
    wd = _wd(ex, engine, tmp_path, bot)

    first = _tick(wd, engine)
    assert first.repaired == [BTC]
    assert first.repair_failures == []
    assert pings, "the healthcheck ping must still go out"

    _tick(wd, engine)  # the next tick must see the stop, not place a second one
    stops = [a for a in ex.algos.values() if a.kind == "sl" and a.state == "live"]
    assert len(stops) == 1


def test_a_balance_error_still_pings_and_dispatches_alerts(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pings: list[str]
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)

    def broken() -> dict[str, Any]:
        raise RuntimeError("balance endpoint 500")

    ex.fetch_balance = broken  # type: ignore[method-assign]
    alerts_seen: list[object] = []
    real_dispatch = watchdog_module.alerts.dispatch
    monkeypatch.setattr(
        watchdog_module.alerts,
        "dispatch",
        lambda items, notifier=None, **kw: (
            alerts_seen.append(items) or real_dispatch(items, notifier, **kw)
        ),
    )

    result = _tick(_wd(ex, engine, tmp_path, RecordingBot()), engine)

    assert result.repaired == [BTC], "the repair before the balance step must stand"
    assert not result.ok, "an unevaluated kill-switch is not a clean pass"
    assert pings and pings[-1].endswith("/fail")
    assert alerts_seen, "alert dispatch must still run"


def test_one_position_that_breaks_its_repair_does_not_strand_the_next(
    engine: Engine, tmp_path: Path, pings: list[str]
) -> None:
    ex = FakeExchange(marks={BTC: Decimal("75000"), ETH: Decimal("3000")})
    ex.open_unprotected(BTC)
    ex.open_unprotected(ETH)
    real_fetch = ex.fetch_positions

    def one_row_unreadable() -> list[dict[str, Any]]:
        rows = real_fetch()
        for row in rows:
            if row["symbol"] == BTC:
                row["entry_price"] = None  # a venue row we cannot do arithmetic on
        return rows

    ex.fetch_positions = one_row_unreadable  # type: ignore[method-assign]
    result = _tick(_wd(ex, engine, tmp_path, RecordingBot()), engine)

    assert BTC in result.repair_failures
    assert ETH in result.repaired
    assert pings
