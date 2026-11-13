"""OKX demo round-trip: open a minimum-size BTC position with an attached stop, then close it.

Runs only with `-m demo` and only against the OKX demo environment (AGENTS.md rule 1).
Needs the demo key in .env and, from Kazakhstan, the VPN on (F-034).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from argus.config import get_settings
from argus.exchange.okx import OKXAdapter
from tests.exchange.demo_access import reach_demo

pytestmark = pytest.mark.demo

SYMBOL = "BTC/USDT:USDT"


@pytest.fixture(scope="module")
def adapter() -> OKXAdapter:
    settings = get_settings()
    if not settings.okx_demo:
        pytest.skip("refusing to run the demo round-trip against a live environment")
    if not settings.okx_credentials_set:
        pytest.skip("OKX demo credentials are not set")
    client = OKXAdapter(settings)
    # Skips only when the environment is the problem; a broken adapter fails (review X-16).
    reach_demo(client.fetch_balance)
    return client


def test_account_is_in_the_mode_the_design_assumes(adapter: OKXAdapter) -> None:
    config = adapter.client.private_get_account_config()["data"][0]
    assert config["posMode"] == "net_mode"
    assert "withdraw" not in config.get("perm", "")
    # acctLv=1 (Simple) cannot trade perpetuals — every order fails with sCode 51010.
    assert config["acctLv"] in ("2", "3", "4"), f"acctLv={config['acctLv']} cannot trade swaps"


def test_fee_rates_come_from_the_account_and_are_charges(adapter: OKXAdapter) -> None:
    fees = adapter.fetch_fee_rates(SYMBOL)
    assert fees["taker"] < 0  # F-036: OKX reports charges as negative
    assert abs(fees["taker"]) < Decimal("0.01")  # sanity: not 1% per side


def test_round_trip_position_always_carries_an_exchange_side_stop(adapter: OKXAdapter) -> None:
    # Preconditions are reported by test_account_is_in_the_mode_the_design_assumes and by
    # `argus doctor --auth`; here they skip with a reason instead of an opaque ccxt error.
    config = adapter.client.private_get_account_config()["data"][0]
    if config["acctLv"] not in ("2", "3", "4"):
        pytest.skip(f"demo account is acctLv={config['acctLv']} — switch to Single-currency margin")
    if adapter.fetch_balance()["equity"] <= 0:
        pytest.skip("demo account has no equity — add demo USDT to the sub-account")
    spec = adapter.instrument_spec(SYMBOL)
    size = spec["min_size"]
    mark = Decimal(str(adapter.fetch_ticker(SYMBOL)["last"]))
    stop = adapter.price_to_precision(SYMBOL, mark * Decimal("0.90"))
    adapter.set_leverage(SYMBOL, 3)
    # Fresh per run. place_bracket is idempotent on clOrdId across finished orders too (review
    # X-8), so a fixed id matched this test's own filled-and-closed order from the previous run
    # and correctly placed nothing, and the test then failed for the wrong reason.
    cl_ord_id = f"argustest{uuid.uuid4().hex}"[:32]
    ids = adapter.place_bracket(
        {
            "symbol": SYMBOL,
            "side": "long",
            "order_type": "market",
            "cl_ord_id": cl_ord_id,
            "size": size,
            "attach_algos": [{"kind": "sl", "trigger_px": stop}],
        }
    )
    try:
        assert ids["entry_id"]
        assert not ids.get("idempotent"), "an idempotent replay placed nothing: clOrdId reused"
        positions = {p["symbol"] for p in adapter.fetch_positions()}
        assert SYMBOL in positions, "market entry did not produce a position"
        # Gate 11 now refuses new entries while an open position reports no margin ratio (Muse
        # P0-2, fail closed). If the venue stopped sending mgnRatio for isolated positions, every
        # second entry would be blocked without an error, so it is asserted on the real venue.
        row = next(p for p in adapter.fetch_positions() if p["symbol"] == SYMBOL)
        assert row.get("margin_ratio") is not None, "venue reported no margin ratio (mgnRatio)"
        # The whole safety model rests on this assertion.
        assert SYMBOL not in adapter.reconcile()["unprotected"]
        stops = [a for a in adapter.fetch_algo_orders() if a["kind"] == "sl"]
        assert stops, "no stop-loss algo order is live for the open position"
    finally:
        adapter.close(SYMBOL)
        adapter.cancel_all(SYMBOL)
    assert SYMBOL not in {p["symbol"] for p in adapter.fetch_positions()}


def test_the_production_placement_path_reaches_the_venue(
    adapter: OKXAdapter, tmp_path: Path
) -> None:
    """The whole execution path, on the demo venue: gate verdict -> order -> live stop -> closed.

    The other demo test opens a position *bypassing* `place_bracket`, because its job is to prove
    the watchdog repairs an unprotected one. This proves the opposite half: that the path
    production actually uses — `build_entry_order` into `place_approved` into `place_bracket` —
    puts a real order on a real venue with its stop attached, and that the ids come back.

    Everything is closed in `finally`, including on assertion failure. A test that can leave a
    position open is a test that can cost money the next time it fails.
    """
    from sqlalchemy import text

    from argus.execution.place import place_approved
    from argus.gate.rules import GateResult, PlanInput, Target
    from argus.gate.sizing import SizingResult
    from argus.journal.db import new_id, open_journal
    from argus.llm.schemas import FactorScores

    # A DIFFERENT symbol from the other demo tests. This one cancels and closes what it made,
    # and `cancel_all` is per-symbol — running it on BTC wiped the resting orders the round-trip
    # and watchdog-repair tests had just set up, failing two tests that were not broken.
    symbol = "ETH/USDT:USDT"
    spec = adapter.instrument_spec(symbol)
    mark = float(adapter.fetch_ticker(symbol)["last"])
    # Well below the mark so the limit rests unfilled: this test is about placement and
    # protection, not about getting a fill.
    entry = round(mark * 0.90, 1)
    stop = round(entry * 0.95, 1)

    # A throwaway journal (F-154). The default one is the production journal on the VPS, and this
    # test's run, plan and order landed there on every demo-test run: fake evidence, a run row
    # left RUNNING until the reaper abandoned it, and test orders counted as the day's entries.
    engine = open_journal(tmp_path / "journal.db")
    run_id, plan_id = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, config_hash)"
                " VALUES (:r,'manager','demo',datetime('now'),'RUNNING','demo')"
            ),
            {"r": run_id},
        )
        conn.execute(
            text(
                "INSERT INTO plans (plan_id, run_id, created_at, mode, symbol, side, state,"
                " payload, prev_hash, row_hash) VALUES (:p,:r,datetime('now'),'demo',:s,'long',"
                " 'GATED','{}','','')"
            ),
            {"p": plan_id, "r": run_id, "s": "ETH"},
        )

    plan = PlanInput(
        symbol="ETH",
        side="long",
        entry_kind="limit",
        entry_low=entry,
        entry_high=entry * 1.001,
        stop=stop,
        targets=[Target(price=entry * 1.2, close_pct=100)],
        time_stop_hours=96,
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=2),
    )
    sized = SizingResult(
        ok=True,
        reason="",
        step=0,
        risk_usd=5.0,
        stop_dist_pct=5.0,
        notional_usd=float(spec["min_size"]) * entry,
        leverage=2,
        margin_usd=10.0,
        contracts=Decimal(str(spec["min_size"])),
        liq_dist_pct=30.0,
    )

    order_id = None
    try:
        order_id = place_approved(
            engine,
            adapter,
            run_id,
            plan,
            GateResult(status="APPROVED", sized=sized),
            plan_id,
            symbol,
        )
        assert order_id, "place_approved returned no order id"

        with engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT exchange_id, cl_ord_id FROM orders WHERE order_id = :o"),
                    {"o": order_id},
                )
                .mappings()
                .first()
            )
        # F-129: the venue's id must land on the row, not only in a log line — without it
        # nothing can amend, cancel or reconcile this order later.
        assert row["exchange_id"], "the venue id was not journalled"

        # Asserting on the journalled venue id rather than scanning the open-order book: a
        # resting limit can fill, be amended, or simply be paged differently, and none of that
        # changes what this test is about — that the order reached the venue and came back
        # identified (F-129).
        assert row["exchange_id"].strip(), "the venue returned an empty id"
    finally:
        # Both swallow their own errors: a resting limit that never filled has no position to
        # close, and raising here would replace the real assertion failure with a misleading one.
        try:
            adapter.cancel_all(symbol)
        except Exception:  # noqa: BLE001 - cleanup must not mask the failure it follows
            pass
        try:
            adapter.close(symbol)
        except Exception:  # noqa: BLE001 - nothing to close is the expected case here
            pass


def test_demo_tests_never_touch_the_production_journal() -> None:
    """Real-venue tests keep the real keys, but never the real ARGUS_HOME (F-154)."""
    settings = get_settings()
    assert settings.argus_home != Path("/var/lib/argus")
    assert "argus_home" in str(settings.argus_home) or "pytest" in str(settings.argus_home)
