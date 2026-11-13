"""Watchdog scenarios on FakeExchange — the safety net, tested where it must not fail."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from argus.config import Settings
from argus.exchange.breakers import BreakerRegistry
from argus.exchange.fake import FakeExchange
from argus.journal import ledger
from argus.journal.db import open_journal, start_run
from argus.notify.telegram import Notifier, RecordingBot
from argus.runs.watchdog import (
    DD_HALT_PCT,
    Watchdog,
    WatchdogResult,
    atr_from_ohlcv,
    read_halt,
    stop_for,
    write_halt,
)

BTC = "BTC-USDT-SWAP"


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(mode="demo", argus_home=tmp_path)


@pytest.fixture
def bot() -> RecordingBot:
    return RecordingBot()


def _watchdog(
    exchange: FakeExchange,
    engine: Engine,
    settings: Settings,
    bot: RecordingBot,
    **kw: object,
) -> Watchdog:
    return Watchdog(
        exchange=exchange,
        engine=engine,
        notifier=Notifier(bot=bot, chat_id="42"),
        settings=settings,
        **kw,  # type: ignore[arg-type]
    )


def _run_id(engine: Engine) -> str:
    with start_run(engine, "watchdog", "demo") as rid:
        return rid


def test_position_without_a_stop_is_repaired_and_alerted(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    assert result.repaired == [BTC]
    assert ex.reconcile()["unprotected"] == []
    assert any("Stop repaired" in text for _, text in bot.sent)


def test_repair_uses_the_plan_stop_when_there_is_one(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    wd = _watchdog(ex, engine, settings, bot, plan_stops={("btc", "long"): Decimal("70000")})
    wd.run_once(_run_id(engine))
    stop = next(a for a in ex.fetch_algo_orders() if a["kind"] == "sl")
    assert stop["trigger_px"] == Decimal("70000")


def test_repair_falls_back_to_two_atr_when_no_plan_exists(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    stop = next(a for a in ex.fetch_algo_orders() if a["kind"] == "sl")
    assert stop["trigger_px"] is not None
    assert stop["trigger_px"] < ex.marks[BTC]  # below entry for a long


def test_failed_repair_is_a_p1_alert_not_a_crash(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    original = ex.place_stop

    def explode(symbol: str, new_stop: Decimal) -> str:
        raise RuntimeError("venue rejected the algo")

    ex.place_stop = explode  # type: ignore[method-assign]
    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    ex.place_stop = original  # type: ignore[method-assign]
    assert result.repair_failures == [BTC]
    assert not result.ok
    assert any("repair FAILED" in text for _, text in bot.sent)


def test_repair_is_journalled_as_a_protection_transition(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT machine, to_state, reason FROM position_events")
        ).fetchall()
    assert rows[0].machine == "protection"
    assert rows[0].to_state == "SL_ATTACHED"
    assert ledger.verify(engine, "position_events") == (True, None)


def test_exchange_down_alerts_only_and_never_raises(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    breakers = BreakerRegistry(threshold=3)
    wd = _watchdog(ex, engine, settings, bot, breakers=breakers)
    for _ in range(3):
        ex.fail_next = TimeoutError("venue down")
        result = wd.run_once(_run_id(engine))
        assert result.unreachable
    assert breakers.get("exchange").state == "open"
    assert any("unreachable" in text for _, text in bot.sent)


def test_drawdown_breach_closes_everything_and_writes_halt(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange(equity=Decimal("1000"))
    ex.open_unprotected(BTC)
    ex.amend_stop(BTC, Decimal("70000"))
    wd = _watchdog(ex, engine, settings, bot)
    wd.run_once(_run_id(engine))  # establishes the 30d high at 1000

    ex.equity = Decimal("800")  # -20%, past the 15% kill-switch
    result = wd.run_once(_run_id(engine))

    assert result.killed
    assert result.drawdown_pct is not None and result.drawdown_pct >= DD_HALT_PCT
    assert ex.fetch_positions() == []
    assert read_halt(settings.halt_file) is not None
    assert any("HALT" in text for _, text in bot.sent)


def test_drawdown_inside_the_limit_does_not_halt(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange(equity=Decimal("1000"))
    wd = _watchdog(ex, engine, settings, bot)
    wd.run_once(_run_id(engine))
    ex.equity = Decimal("900")  # -10%
    result = wd.run_once(_run_id(engine))
    assert not result.killed
    assert read_halt(settings.halt_file) is None


def test_an_existing_halt_file_stops_the_run_from_trading_but_still_reports(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    write_halt(settings.halt_file, "manual /halt")
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    assert result.halted
    assert result.repaired == [BTC]  # protection is repaired even while halted
    assert result.drawdown_pct is None  # but no kill-switch evaluation


def test_halt_file_write_is_atomic(settings: Settings) -> None:
    write_halt(settings.halt_file, "first")
    assert not settings.halt_file.with_suffix(".tmp").exists()
    assert "first" in (read_halt(settings.halt_file) or "")


def test_stop_for_places_the_default_on_the_correct_side() -> None:
    long_pos = {"side": "long", "entry_price": Decimal("100")}
    short_pos = {"side": "short", "entry_price": Decimal("100")}
    atr = Decimal("2")
    assert stop_for(long_pos, None, atr) == Decimal("96")
    assert stop_for(short_pos, None, atr) == Decimal("104")
    assert stop_for(long_pos, Decimal("91"), atr) == Decimal("91")


def test_atr_needs_enough_candles() -> None:
    assert atr_from_ohlcv([[0, 1, 2, 0.5, 1, 10]] * 5) is None
    candles = [[float(i), 100.0, 102.0, 98.0, 100.0, 10.0] for i in range(20)]
    atr = atr_from_ohlcv(candles)
    assert atr is not None and atr > 0


# --- antigravity review H1 (reproduced 2026-09-17) ---------------------------------------


def test_repair_stop_is_never_zero_or_negative() -> None:
    # Reproduced: a 60%-of-entry ATR put a long's emergency stop at -20. OKX rejects that,
    # the repair fails, and the position stays naked.
    long_pos = {"side": "long", "entry_price": Decimal("100"), "leverage": 3}
    stop = stop_for(long_pos, None, Decimal("60"))
    assert stop > 0


def test_repair_stop_sits_inside_the_liquidation_price() -> None:
    # Worse than the review's case: at 10x a 2xATR stop 16% away is *accepted* by OKX but can
    # never fire, because isolated margin liquidates ~10% away. Silently unprotected.
    for side in ("long", "short"):
        position = {"side": side, "entry_price": Decimal("100"), "leverage": 10}
        stop = stop_for(position, None, Decimal("8"))
        distance_pct = abs(stop - Decimal("100"))
        assert distance_pct <= Decimal("5"), f"{side} stop {stop} is beyond half the liq distance"


def test_a_tight_atr_stop_is_left_alone_by_the_liquidation_clamp() -> None:
    position = {"side": "long", "entry_price": Decimal("100"), "leverage": 3}
    assert stop_for(position, None, Decimal("2")) == Decimal("96")


def test_a_position_that_already_has_a_stop_is_left_alone(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    # Lesson from the nautilus project (its F044): on restart it placed a *second* stop on a
    # position that already had one, OKX refused it, and the guard closed the position.
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    ex.amend_stop(BTC, Decimal("70000"))
    before = [a["algo_id"] for a in ex.fetch_algo_orders()]

    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))

    assert result.repaired == [] and result.repair_failures == []
    after = ex.fetch_algo_orders()
    assert [a["algo_id"] for a in after] == before  # same algo, not a second one
    assert len([a for a in after if a["kind"] == "sl"]) == 1
    assert not any("Stop repaired" in text for _, text in bot.sent)


def test_unprotected_position_raises_a_critical_alert(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """The `positions_unprotected` alert is the last line of AGENTS.md rule 4."""
    ex = FakeExchange()
    ex.open_unprotected(BTC)

    def explode(symbol: str, new_stop: Decimal) -> str:
        raise RuntimeError("venue rejected the algo")

    ex.place_stop = explode  # type: ignore[method-assign]
    _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    assert any("CRITICAL] positions_unprotected" in text for _, text in bot.sent)


def test_a_clean_pass_raises_no_protection_alert(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    assert not any("positions_unprotected" in text for _, text in bot.sent)


def test_an_empty_halt_file_still_halts_the_watchdog(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """Review 2026-09-18 X-5: `touch HALT` is the only halt method an operator has today.

    The manager treats existence as a halt (`halt_path.exists()`); the watchdog read the text and
    then tested it for truthiness, so an empty file halted the manager and not the watchdog —
    which went on to run the drawdown branch that can close every position.
    """
    settings.halt_file.parent.mkdir(parents=True, exist_ok=True)
    settings.halt_file.write_text("", encoding="utf-8")
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    assert result.halted is True
    assert read_halt(settings.halt_file)  # a reason is always reported, even when the file is empty


def test_a_stop_below_the_current_mark_closes_instead_of_placing_an_impossible_order(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """Review 2026-09-18 X-2: the repair stop was never compared with the current price.

    `stop_for` derives the stop from the entry price. If the market has already crossed it — which
    is exactly when protection matters — OKX rejects the order, the repair fails, and the position
    stays naked while every tick retries the same impossible order.
    """
    ex = FakeExchange()
    ex.open_unprotected(BTC)  # long, entry 100
    ex.marks[BTC] = Decimal("80")  # already far below any entry-derived stop
    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))

    assert BTC not in ex.positions, "a position past its stop must be closed, not left naked"
    assert result.repair_failures == []
    assert any("closed" in text.lower() for _, text in bot.sent)


def test_a_thin_margin_ratio_warns(engine: Engine, settings: Settings, bot: RecordingBot) -> None:
    """Review 2026-09-18 X-7: the margin floor never fired because nothing supplied the ratio.

    The adapter now maps OKX's `mgnRatio`, and the fake carries it too — without that, this check
    passes in tests while staying dead in production, which is how it went unnoticed.
    """
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    ex.positions[BTC].margin_ratio = Decimal("150")  # floor is 200
    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    assert result.margin_warnings == [BTC]
    assert any("margin" in text.lower() for _, text in bot.sent)


def test_a_healthy_margin_ratio_is_silent(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    ex.positions[BTC].margin_ratio = Decimal("900")
    result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
    assert result.margin_warnings == []


def _armed(ex: FakeExchange, engine: Engine, settings: Settings, bot: RecordingBot) -> Watchdog:
    """A watchdog whose 30-day peak is established, with equity already 20% below it."""
    ex.open_unprotected(BTC)
    ex.amend_stop(BTC, Decimal("70"))
    wd = _watchdog(ex, engine, settings, bot)
    wd.run_once(_run_id(engine))  # records the peak at the starting equity
    ex.equity = ex.equity * Decimal("0.8")  # -20%, past the 15% kill-switch
    return wd


def test_the_kill_switch_writes_halt_before_it_closes_anything(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """Review 2026-09-18 X-4(2): HALT was written *after* the close loop.

    A crash or a systemd timeout during the closes (N x 20s ccxt timeouts is easy to reach) left
    no HALT behind, so the manager carried on opening positions into the drawdown that had just
    triggered a flatten.
    """
    ex = FakeExchange(equity=Decimal("1000"))
    wd = _armed(ex, engine, settings, bot)
    halt_seen: list[bool] = []
    original_close = ex.close

    def watched_close(symbol: str) -> str:
        halt_seen.append(settings.halt_file.exists())
        return original_close(symbol)

    ex.close = watched_close  # type: ignore[method-assign]
    wd.run_once(_run_id(engine))

    assert halt_seen, "the kill-switch must still close positions"
    assert all(halt_seen), "HALT must already exist when the first close is attempted"


def test_resting_entries_are_cancelled_before_the_flatten(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """X-4(3): unfilled limit entries were left live and could fill *after* the HALT.

    OKX's `autoCxl` only covers the closing orders attached to a position, never a resting entry.
    """
    ex = FakeExchange(equity=Decimal("1000"))
    wd = _armed(ex, engine, settings, bot)
    events: list[str] = []
    original_cancel, original_close = ex.cancel_all, ex.close
    ex.cancel_all = lambda s: events.append(f"cancel:{s}") or original_cancel(s)  # type: ignore[assignment,func-returns-value]
    ex.close = lambda s: (events.append(f"close:{s}"), original_close(s))[1]  # type: ignore[assignment]
    wd.run_once(_run_id(engine))

    assert events.index(f"cancel:{BTC}") < events.index(f"close:{BTC}")


def test_the_kill_switch_cancels_a_resting_entry_on_a_flat_symbol(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """X-N4: the cancel loop ran over positions only, so a limit entry on a symbol with no
    position survived the flatten and could open a fresh position after the HALT."""
    ex = FakeExchange(equity=Decimal("1000"))
    wd = _armed(ex, engine, settings, bot)
    entry = ex.place_bracket(
        {
            "symbol": "ETH-USDT-SWAP",
            "side": "buy",
            "price": "100",
            "size": "1",
            "cl_ord_id": "resting1",
            "attach_algos": [],
        }
    )["entry_id"]
    wd.run_once(_run_id(engine))

    assert ex.fetch_positions() == []
    assert ex.orders[entry].state == "CANCELLED", "the resting entry survived the kill-switch"


def test_a_failed_close_is_alerted_and_retried_on_the_next_tick(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """X-4(1): close failures only reached `notes`, and the next tick skipped the whole branch.

    `halted=True` made `_drawdown_check` return early, so a position that refused to close once
    was never closed at all — the kill-switch reported success while the risk stayed open.
    """
    ex = FakeExchange(equity=Decimal("1000"))
    wd = _armed(ex, engine, settings, bot)
    working = ex.close

    def refuse(symbol: str) -> str:
        raise RuntimeError("venue rejected close")

    ex.close = refuse  # type: ignore[method-assign]
    first = wd.run_once(_run_id(engine))
    assert first.close_failures == [BTC]
    assert not first.ok
    assert any("close" in text.lower() for _, text in bot.sent)

    ex.close = working  # type: ignore[method-assign]
    wd.run_once(_run_id(engine))
    assert ex.fetch_positions() == [], "a halted tick must retry the flatten it failed to finish"


def test_an_unreachable_venue_with_a_known_flat_book_is_not_a_failure(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OKX puts single algo-order types into maintenance, and reconcile refuses partial answers.

    That strictness is right — a partial algo list can make a protected position look naked. But
    it made every maintenance window a failed unit: three in three days with nothing open
    (F-140). An operator who sees `Failed` for routine venue weather learns to ignore the one
    that matters.

    The distinction is whether money was on the table. We cannot ask the venue when it is
    unreachable, so we ask the journal: `positions_snapshots` holds the last book we saw.
    """
    from argus.journal import positions as positions_journal

    result = WatchdogResult(run_id="r1")
    result.unreachable = True
    result.notes.append("reconcile failed: algo-order query failed for ordType=oco: OnMaintenance")

    # Nothing was open when we last looked, and nothing has been placed since.
    assert positions_journal.latest_open(engine, venue="demo") == {}
    assert result.degraded_not_failed(engine, venue="demo") is True
    assert result.ok is False, "still not a clean pass — the venue was not reached"


def test_an_unreachable_venue_with_a_position_open_is_a_failure(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unverified protection with money on the table is an incident, not weather."""
    from argus.journal import positions as positions_journal

    run_id = "wd-seed"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                " VALUES (:r,'watchdog','demo',datetime('now'),'OK','demo')"
            ),
            {"r": run_id},
        )
    positions_journal.record(
        engine,
        run_id,
        [
            {
                "symbol": "SOL",
                "side": "long",
                "size": "3",
                "entry_price": 100.0,
                "mark_price": 104.0,
                "leverage": 2,
                "unrealised": "12",
            }
        ],
    )

    result = WatchdogResult(run_id="r1")
    result.unreachable = True
    assert result.degraded_not_failed(engine, venue="demo") is False


def test_a_repair_failure_is_never_downgraded(engine: Engine) -> None:
    """Reaching the venue and failing to protect something is the worst case, always."""
    result = WatchdogResult(run_id="r1")
    result.unreachable = True
    result.repair_failures.append("SOL")
    assert result.degraded_not_failed(engine, venue="demo") is False


def test_a_close_is_booked_at_the_pnl_the_venue_realised(
    engine: Engine, settings: Settings, bot: RecordingBot
) -> None:
    """The last snapshot is up to 20 minutes old. A position that stopped out after it must be
    journalled at what the venue booked, which is what gate 9's loss limits then read."""
    import json

    ex = FakeExchange()
    ex.open_unprotected(BTC)
    ex.amend_stop(BTC, Decimal("70000"))
    watchdog = _watchdog(ex, engine, settings, bot)
    watchdog.run_once(_run_id(engine))

    ex.positions.pop(BTC)  # stopped out between ticks
    ex.realised[BTC] = Decimal("-37.5")
    watchdog.run_once(_run_id(engine))

    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT raw FROM positions_snapshots WHERE state = 'CLOSED'")
        ).scalar_one()
    assert json.loads(raw)["realised_pnl"] == "-37.5"


def test_an_unreachable_venue_with_a_position_open_is_reported_every_hour(
    engine: Engine, settings: Settings, bot: RecordingBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every tick is a fresh process, so the in-memory breaker never reaches its threshold and
    `exchange_unreachable` cannot fire; `breaker_open` is the only message. It must not go quiet
    for six hours while stops and the drawdown kill-switch are blind (review O-3 follow-up)."""
    from datetime import UTC, datetime, timedelta

    from argus.observability import alerts

    t0 = datetime(2026, 10, 1, tzinfo=UTC)

    class Clock(datetime):
        current = t0

        @classmethod
        def now(cls, tz: object = None) -> Clock:  # type: ignore[override]
            return cls.current

    monkeypatch.setattr(alerts, "datetime", Clock)
    ex = FakeExchange()
    ex.open_unprotected(BTC)
    ex.amend_stop(BTC, Decimal("70000"))
    _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))  # the book is now on record

    for minutes in range(20, 3 * 60 + 1, 20):  # three hours of 20-minute ticks
        Clock.current = t0 + timedelta(minutes=minutes)
        ex.fail_next = TimeoutError("venue down")
        result = _watchdog(ex, engine, settings, bot).run_once(_run_id(engine))
        assert result.unreachable

    reported = [text for _, text in bot.sent if "breaker_open" in text]
    assert len(reported) >= 3
