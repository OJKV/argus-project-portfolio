"""ARGUS command-line entrypoints (ARCHITECTURE.md §12)."""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

from argus import __version__
from argus.config import APPROVE_IS_AUTO, get_settings

if TYPE_CHECKING:
    from argus.doctor import Check

app = typer.Typer(help="ARGUS — LLM-assisted swing-trading agent", no_args_is_help=True)
console = Console()

# EX_TEMPFAIL: the watchdog could not take the run lock on consecutive ticks. Distinct from 1
# (the run itself failed) so the journal and the unit log say which (J-4).
SKIPPED_EXIT = 75


def _telemetry(service: str, engine: Any = None) -> None:
    """Start logging and tracing for this process. Tracing is a no-op without LOGFIRE_TOKEN.

    Logging first: JSON lines with the run_id in ARGUS_HOME/logs, human lines on the console
    (O-4). Neither step can fail a run.
    """
    from argus import observability
    from argus.observability.logs import configure_logging

    configure_logging(get_settings().argus_home, service)
    if observability.configure(service) and engine is not None:
        observability.instrument_engine(engine)


def _print(title: str, checks: list[Check]) -> int:
    table = Table(title=title)
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    failed = 0
    for c in checks:
        table.add_row(c.name, "[green]OK[/green]" if c.ok else "[red]FAIL[/red]", c.detail)
        failed += 0 if c.ok else 1
    console.print(table)
    return failed


@app.command()
def version() -> None:
    console.print(f"argus {__version__}")


@app.command()
def doctor(
    network: bool = typer.Option(True, help="Probe public endpoints"),
    universe: bool = typer.Option(False, help="Verify core universe against OKX instruments"),
    models: bool = typer.Option(False, help="Ping every model in config/models.yaml (slow)"),
    auth: bool = typer.Option(False, help="Authenticated OKX read-only check (needs VPN from KZ)"),
    reconcile_migrations: bool = typer.Option(
        False,
        "--reconcile-migrations",
        help="Re-record checksums for applied migrations whose files changed (no schema change)",
    ),
    live_keys: bool = typer.Option(
        False,
        "--live-keys",
        help="Read-only check of the LIVE OKX keys. Places nothing, changes no setting.",
    ),
    telegram: bool = typer.Option(
        False, "--telegram", help="Send one test alert and explain any failure in words"
    ),
) -> None:
    """Config, keys presence and connectivity. Exit code = number of failures."""
    from argus import doctor as d

    if telegram:
        from argus.notify.telegram import diagnose

        ok, detail = diagnose()
        console.print(f"[{'green' if ok else 'red'}]{'OK' if ok else 'FAILED'}[/] {detail}")
        raise typer.Exit(code=0 if ok else 1)

    if reconcile_migrations:
        from argus.journal.db import make_engine
        from argus.journal.db import reconcile_migrations as reconcile

        blessed = reconcile(make_engine())
        console.print(f"re-recorded: {', '.join(blessed)}" if blessed else "nothing to reconcile")
        raise typer.Exit(code=0)

    failed = _print("config", d.check_config())
    failed += _print("keys (presence only)", d.check_keys())
    if network:
        failed += _print("public endpoints", d.check_public())
        if logfire := d.check_logfire():
            failed += _print("Logfire project", logfire)
    if universe:
        failed += _print("core universe on OKX", d.check_universe_okx())
    if auth:
        failed += _print("OKX authenticated (read-only)", d.check_okx_auth())
    if live_keys:
        # Explicitly the live triple, whatever MODE says. Read endpoints only: account
        # config, balance and fee rates. Nothing is placed, amended or cancelled.
        failed += _print("OKX LIVE keys (read-only)", d.check_okx_auth(env="live"))
    if models:
        failed += _print("LLM models (ping)", d.check_models())
    raise typer.Exit(code=min(failed, 255))


def _not_yet(name: str) -> None:
    console.print(f"[yellow]{name}: not implemented yet — see docs/BUILD_PLAN.md[/yellow]")
    raise typer.Exit(code=2)


def _executor(engine: Any) -> Any:
    """The live executor: one OKX adapter, reused, handed to the manager only on a live run.

    Built here rather than inside `place_approved` so that a shadow run has no adapter at all.
    The absence of a venue is a stronger guarantee than a flag saying not to use one.
    """
    from argus.exchange.okx import OKXAdapter
    from argus.execution.place import place_approved

    adapter = OKXAdapter()

    def execute(run_id: str, plan: Any, gate: Any, plan_id: str, instrument: str) -> str | None:
        return place_approved(engine, adapter, run_id, plan, gate, plan_id, instrument)

    return execute


# Set in the environment of a run that restarted itself after a deploy, so it restarts once.
_RESTARTED_ENV = "ARGUS_RESTARTED_AFTER_DEPLOY"


def _restart_on_new_code(exc: Exception) -> NoReturn:
    """Start this same command again, on the code a deploy has just put in place.

    A run that queued behind `update.sh` for the lock has already imported half of ARGUS from the
    old checkout and would import the rest from the new one (review 2026-10-07, second pass).
    Exiting would lose the slot — nothing retries a manager for twelve hours — so it starts over
    with the same argv in a fresh interpreter. Once only: a second deploy inside the restarted
    run's wait gives the slot up rather than loop.
    """
    import os
    import sys

    if os.environ.get(_RESTARTED_ENV) == "1":
        console.print(f"[yellow]{exc} — already restarted once; giving this slot up[/yellow]")
        raise typer.Exit(code=0)
    console.print(f"[yellow]{exc} — starting again on the new code[/yellow]")
    os.environ[_RESTARTED_ENV] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, sys.orig_argv)


@app.command()
def manager(
    shadow: bool = typer.Option(False, "--shadow", help="Research and gate, place nothing"),
    symbols: str = typer.Option("", help="Comma-separated override of the research set"),
    equity: float = typer.Option(
        0.0, help="Shadow-only equity override when the exchange is unreachable"
    ),
    llm: bool = typer.Option(
        False, "--llm", help="Use the real Trader agent instead of the deterministic stub"
    ),
    scheduled: bool = typer.Option(
        False,
        "--scheduled",
        help="Started by argus-manager.timer: a live account skips the demo-only slots",
    ),
) -> None:
    """Manager run: regime → universe → dossiers → proposal → Risk Gate → journal."""
    settings_now = get_settings()
    if scheduled:
        # Q6: the timer fires six times a day, the live account runs at 00:30/12:30 UTC only.
        # Checked first, on the start time, before the lock wait or any venue call.
        from argus.runs.schedule import scheduled_run_refusal

        refusal = scheduled_run_refusal(settings_now.venue)
        if refusal is not None:
            console.print(f"[yellow]{refusal}[/yellow]", soft_wrap=True)
            raise typer.Exit(code=0)
    if not shadow and equity > 0:
        # An armed run gated against an invented balance and an empty book is the failure the
        # whole book wiring exists to prevent (review G-N1). The override is for offline shadows.
        console.print(
            "[red]--equity is shadow-only.[/red] An armed run reads the real account; "
            "use --shadow to try an equity."
        )
        raise typer.Exit(code=2)
    if not shadow:
        # `argus.config` already refuses MODE=auto|approve without LIVE_TRADING=I_UNDERSTAND, so
        # by the time we are here the interlock has been satisfied. What this adds is that a live
        # run must be *asked for* — MODE is a deployment setting, and nobody should discover they
        # armed execution by forgetting a flag.
        if settings_now.mode not in ("approve", "auto"):
            console.print(
                f"[red]MODE={settings_now.mode} does not place orders.[/red] "
                "Use --shadow, or set MODE=approve|auto and LIVE_TRADING=I_UNDERSTAND."
            )
            raise typer.Exit(code=2)
        if not llm:
            # The stub scores every asset +3 against a +4 bar, so an armed stub run is a placebo
            # that looks exactly like a strict gate refusing a dull market. The scheduled unit
            # ran this way for its first day (F-145). Shadow runs may still use it.
            console.print(
                "[red]An armed run needs the real agents.[/red] Add --llm, or use --shadow "
                "for the deterministic stub."
            )
            raise typer.Exit(code=2)
        if equity > 0:
            # Documented shadow-only and honoured armed, so the passwordless `trade.sh --equity
            # 1000000` sized real orders on an invented balance (review 2026-10-07, second pass).
            # Whenever orders can be sent, sizing reads the venue's own equity.
            console.print(
                "[red]--equity is for --shadow runs only.[/red] An armed run sizes on the "
                "account's real equity; add --shadow, or drop --equity."
            )
            raise typer.Exit(code=2)
        venue = settings_now.venue
        colour = "yellow" if venue == "demo" else "bold red"
        console.print(
            f"[{colour}]ARMED · {venue.upper()} account[/{colour}] mode={settings_now.mode}"
            + (" — fake funds, real code path" if venue == "demo" else " — REAL MONEY")
        )
        if settings_now.mode == "approve":
            console.print(f"[bold]{APPROVE_IS_AUTO}[/bold] — nothing waits for your yes (P-N2)")

    from argus.data.fetchers import hyperliquid
    from argus.features import market as mkt
    from argus.features.dossier import Dossier
    from argus.features.regime import RegimeView
    from argus.features.universe import Asset
    from argus.gate.rules import MarketContext
    from argus.journal.db import open_journal
    from argus.llm import stub
    from argus.runs.manager import (
        ManagerDeps,
        closed_trades,
        gate_book,
        risk_book,
        run_manager,
        venue_positions,
        venue_stops,
    )

    chosen = _requested_symbols(symbols)
    # The decision profile for this account (DECISION_LOGIC §11): the venue decides, nothing else.
    from argus.gate.limits import limits_for

    limits = limits_for(settings_now.venue)
    engine = open_journal()
    _telemetry("argus-manager", engine)
    listed = {a.symbol: a for a in _listed_assets()}
    assets = (
        [listed.get(s) or Asset(s, f"{s}-USDT-SWAP", "", "satellite") for s in chosen]
        if chosen
        else list(listed.values())
    )
    state: dict[str, Any] = {}

    if llm:
        from argus.llm.agents.trader import propose as llm_propose
        from argus.llm.agents.trader import to_plan_input
        from argus.llm.router import Router
        from argus.llm.schemas import TraderAnswer

        router = Router(engine=engine)

        # Resolved once per run, not per asset: every asset must be judged against the same
        # memory, and a lesson that expired mid-run would make two plans incomparable.
        from argus.journal import lessons as memory

        active_lessons = memory.render(memory.active(engine))

        def book() -> dict[str, Any]:
            """What the Risk Debate needs to judge a trade against the rest of the book.

            Read through a closure rather than passed in: `ProposeFn` takes one dossier because
            the deterministic stub did, and widening that signature belongs with the batched
            Trader (F-117) rather than here. `account` is assigned before any proposer call.
            """
            from datetime import UTC, datetime

            # The gate's book (review D-N2 / G-N7): venue positions plus resting entries, with
            # risk as a share of equity and the recent stop-outs `risk_flags` reads.
            equity_now, _free, held = account
            positions = gate_book(engine, list(held))
            now = datetime.now(UTC)
            return risk_book(equity_now, positions, closed_trades(engine, now), now)

        def propose_batch(run_id: str, candidates: list[Any]) -> dict[str, Any]:
            """One Trader call for the whole cycle (F-117).

            Returns a plan per symbol. A symbol the Trader did not pick is simply absent, which
            the manager reads the same way it reads a per-asset refusal.
            """
            import dataclasses

            from argus.llm.agents import trader_batch
            from argus.llm.router import AllModelsFailedError, agent_policy
            from argus.observability import alerts
            from argus.runs.pipeline import vet

            dossiers = [dossier for _asset, dossier, _market in candidates]
            # Gate 10 refuses a plan on any symbol the book holds; do not spend picks on them.
            held_now = {p.symbol for p in gate_book(engine, list(account[2]))}
            dossiers, skipped_held = trader_batch.drop_held(dossiers, held_now)
            if skipped_held:
                console.print(f"held, not offered to the Trader: {', '.join(skipped_held)}")
            try:
                call = trader_batch.propose(
                    router,
                    run_id,
                    dossiers,
                    book(),
                    lessons=active_lessons,
                    limits=limits,
                )
            except AllModelsFailedError as exc:
                alerts.dispatch(
                    [
                        alerts.agent_failed(
                            "trader", agent_policy("trader").criticality, "batch", str(exc)
                        )
                    ]
                )
                return {}

            answer = call.output
            kept, invented = trader_batch.keep_offered(
                answer, trader_batch.offered_symbols(dossiers)
            )
            if invented:
                # A plan on a symbol nobody researched has no dossier behind it, so nothing
                # downstream could gate it honestly.
                console.print(f"[yellow]dropped plans on unoffered symbols: {invented}[/yellow]")
            if answer.portfolio_note:
                console.print(f"  [dim]trader: {answer.portfolio_note}[/dim]")
            if not kept:
                console.print(f"  no trade: {answer.no_trade_reason}")
                return {}

            by_symbol = {d.symbol: d for d in dossiers}
            out: dict[str, Any] = {}
            for plan in kept:
                single = TraderAnswer(decision="trade", plan=plan)
                checked = vet(
                    router,
                    run_id,
                    by_symbol[plan.symbol],
                    single,
                    trader_family=call.ref.family,
                    portfolio=book(),
                    regime=f"{by_symbol[plan.symbol].regime.label} "
                    f"(risk x{by_symbol[plan.symbol].regime.risk_mult})",
                    lessons=active_lessons,
                    journal=engine,
                    limits=limits,
                )
                console.print(
                    f"  {plan.symbol}: "
                    + ", ".join(f"{a}={o}" for a, o in checked.checks)
                    + (f" -> blocked by {checked.blocked_by}" if checked.blocked_by else "")
                )
                if checked.approved:
                    out[plan.symbol] = dataclasses.replace(
                        to_plan_input(plan),
                        size_factor=checked.size_factor,
                        trader_family=call.ref.family,
                    )
            return out

        def proposer(run_id: str, d: Any, m: Any) -> Any:
            """A refusal is an answer; a failure is not.

            NO_TRADE and "every model failed" both produce no plan, and the gate cannot tell
            them apart — so the difference has to be made here, where the reason is still known.
            One is the system working; the other wakes owner up.
            """
            from argus.llm.router import AllModelsFailedError, agent_policy
            from argus.observability import alerts
            from argus.runs.pipeline import vet

            try:
                call = llm_propose(router, run_id, d, m, lessons=active_lessons, limits=limits)
            except AllModelsFailedError as exc:
                alerts.dispatch(
                    [
                        alerts.agent_failed(
                            "trader", agent_policy("trader").criticality, d.symbol, str(exc)
                        )
                    ]
                )
                return None
            answer = call.output
            if answer.plan is None:
                return None

            # Nothing reaches the gate without a second opinion and a look at the whole book.
            # Both agents are `safety`: an unavailable check drops the trade rather than being
            # skipped, because silence from an agent whose job is to object is not approval.
            checked = vet(
                router,
                run_id,
                d,
                answer,
                trader_family=call.ref.family,
                portfolio=book(),
                regime=f"{d.regime.label} (risk x{d.regime.risk_mult})",
                lessons=active_lessons,
                journal=engine,
                limits=limits,
            )
            console.print(
                f"  {d.symbol}: "
                + ", ".join(f"{agent}={outcome}" for agent, outcome in checked.checks)
                + (f" -> blocked by {checked.blocked_by}" if checked.blocked_by else "")
            )
            if not checked.approved:
                return None
            import dataclasses

            # A factor, never a size. `argus/gate/sizing.py` still decides how much, and clamps
            # this to 1.0 on the way in.
            return dataclasses.replace(
                to_plan_input(answer.plan),
                size_factor=checked.size_factor,
                trader_family=call.ref.family,
            )
    else:

        def proposer(run_id: str, d: Any, m: Any) -> Any:
            return stub.propose(d, m, shadow=True)

    # Resolve the account before the run starts: a control-flow exit inside a run callback
    # would be swallowed by the manager's per-asset error handling.
    account: tuple[float, float, list[Any]]
    if equity > 0:
        account = (equity, equity, [])
        console.print(f"[yellow]using --equity ${equity:,.2f} (shadow only)[/yellow]")
    else:
        from argus.exchange.okx import OKXAdapter

        try:
            adapter = OKXAdapter()
            balance = adapter.fetch_balance()
            # The book the gate judges against (review G-N1): what the venue holds, priced with
            # each instrument's contract value. Any failure here ends the run before research —
            # an armed run that cannot see its own positions must not add to them.
            held = adapter.fetch_positions()
            # The stops actually resting at the venue: a naked position, or one whose stop was
            # moved away, is measured against that rather than the plan's stop.
            stops = venue_stops(adapter.fetch_algo_orders())
            contract_values = {}
            for row in held:
                base = str(row["symbol"]).split("/")[0].split("-")[0].upper()
                spec = mkt.instrument_spec(Asset(base, f"{base}-USDT-SWAP", "", "satellite"))
                contract_values[str(row["symbol"])] = spec.contract_value
            account = (
                float(balance["equity"]),
                float(balance["free"]),
                venue_positions(engine, held, contract_values, stops),
            )
        except Exception as exc:  # noqa: BLE001 - shadow runs may have no exchange access
            console.print(
                f"[red]exchange unreachable ({type(exc).__name__})[/red] — "
                "turn the VPN on, or pass --equity for an offline shadow run"
            )
            raise typer.Exit(code=1) from exc

    def regime(assets_in: list[Asset]) -> tuple[RegimeView, list]:  # type: ignore[type-arg]
        view, prov = mkt.regime_snapshot(assets_in, limits=limits)
        state["btc_daily"], _ = mkt.candles("BTC-USDT-SWAP", "1D", 300)
        state["hl"] = hyperliquid.fetch_meta_and_contexts()
        return view, prov

    def dossier(asset: Asset, view: RegimeView) -> Dossier:
        return mkt.build_dossier(asset, view, state["btc_daily"], state["hl"], engine)

    def market_ctx(asset: Asset, built: Dossier) -> MarketContext:
        # A live quote, not the dossier's closed 4H bar (D-1, D-3). This one is read during
        # research; `quote=mkt.live_quote` below reads it again just before the plan is gated.
        quote = mkt.live_quote(asset)
        # Shadow may gate on published fee defaults; an armed run may not (G-14, F-026).
        fees = mkt.fee_rates(asset, allow_default=shadow)
        return mkt.market_context(
            asset,
            built,
            mkt.instrument_spec(asset),
            fees,
            quote,
            listed=mkt.instrument_listed(asset),
        )

    def last_close(base: str, timeframe: str) -> float | None:
        # The open positions' invalidation rules are judged on CLOSED candles only (§9); the
        # frame drops the forming bar. Daily closes are taken on UTC days, like everything else.
        bar = {"4h": "4H", "1d": "1Dutc"}[timeframe]
        frame, _ = mkt.candles(f"{base}-USDT-SWAP", bar, 5)
        return None if frame.empty else float(frame["close"].iloc[-1])

    deps = ManagerDeps(
        assets=lambda: assets,
        regime=regime,
        dossier=dossier,
        market=market_ctx,
        portfolio=lambda: account,
        propose=proposer,
        propose_batch=propose_batch if llm else None,
        execute=None if shadow else _executor(engine),
        quote=mkt.live_quote,
        last_close=last_close,
    )
    from argus.runs.lock import SCHEDULED_RUN_WAIT_S, CodeChangedError, LockBusyError, run_lock

    try:
        with run_lock("manager", get_settings().argus_home, timeout_s=SCHEDULED_RUN_WAIT_S):
            result = run_manager(engine, deps, shadow=shadow)
    except CodeChangedError as exc:
        _restart_on_new_code(exc)
    except LockBusyError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=0) from exc
    except Exception as exc:
        # Whatever still escapes run_manager — its row write or close hitting a locked journal —
        # is a failed run the operator hears about, not only a traceback in the unit log (J-1).
        from argus.observability import alerts

        error = f"{type(exc).__name__}: {exc}"
        alerts.dispatch([alerts.run_failed("manager", "?", error)], engine=engine)
        raise

    if result.halted:
        console.print("[red]HALT file present — nothing placed after it was seen[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]{result.summary()}[/bold]  run {result.run_id[:8]}")
    table = Table(title="gate outcomes")
    for column in ("symbol", "side", "verdict", "detail"):
        table.add_column(column)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.symbol, p.side, g.verdict, g.reasons FROM plans p"
                " JOIN gate_results g ON g.plan_id = p.plan_id WHERE p.run_id = :r"
            ),
            {"r": result.run_id},
        ).mappings()
        for row in rows:
            payload = _json.loads(row["reasons"])
            last = payload["reasons"][-1] if payload.get("reasons") else {}
            colour = {"APPROVED": "green", "ADJUSTED": "yellow"}.get(row["verdict"], "red")
            table.add_row(
                row["symbol"],
                row["side"],
                f"[{colour}]{row['verdict']}[/{colour}]",
                f"gate {last.get('gate', '?')} {last.get('name', '')}: {last.get('detail', '')}"[
                    :90
                ],
            )
    console.print(table)
    for error in result.errors:
        console.print(f"[red]error[/red] {error}")
    console.print(f"{result.skipped_for_gaps} asset(s) skipped for missing trade-critical data")
    # A run with errors — a setup failure, or a placement the venue refused — exits 1 and says so,
    # as `review` does. Exiting 0 kept the unit green and the operator unaware (J-1 / J-N4).
    if result.errors or result.status == "FAILED":
        from argus.observability import alerts

        alerts.dispatch(
            [alerts.run_failed("manager", result.run_id, "; ".join(result.errors) or "FAILED")],
            engine=engine,
        )
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@app.command()
def review(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Decide and journal, but touch nothing at the venue"
    ),
) -> None:
    """Judge every open position: hold, trim, close, or tighten the stop.

    The only run that can change a position that already exists. It cannot open one — the
    vocabulary has no verb for it — and it cannot widen a stop, which the schema refuses and the
    run re-checks against the venue before sending.
    """
    from argus.data.fetchers import hyperliquid
    from argus.exchange.okx import OKXAdapter
    from argus.features import market as mkt
    from argus.features.universe import Asset
    from argus.journal.db import open_journal, start_run
    from argus.llm.router import Router
    from argus.runs.lock import SCHEDULED_RUN_WAIT_S, CodeChangedError, LockBusyError, run_lock
    from argus.runs.review import ReviewIncompleteError, run_review

    settings = get_settings()
    engine = open_journal()
    _telemetry("argus-review", engine)

    if not dry_run and not settings.is_live:
        console.print(
            f"[yellow]MODE={settings.mode} cannot change a position.[/yellow] "
            "Use --dry-run, or set MODE=approve|auto."
        )
        raise typer.Exit(code=2)
    if not dry_run:
        console.print(
            f"[{'yellow' if settings.venue == 'demo' else 'bold red'}]ARMED · "
            f"{settings.venue.upper()} account[/] — positions may be closed or trimmed"
        )
        if settings.mode == "approve":
            console.print(f"[bold]{APPROVE_IS_AUTO}[/bold] — nothing waits for your yes (P-N2)")

    adapter = OKXAdapter()
    listed = {a.symbol: a for a in _listed_assets()}
    known = _known_assets()
    state: dict[str, Any] = {}

    def dossier_for(symbol: str) -> Any:
        """A FRESH dossier. Reviewing against the one that opened the trade would only ever
        re-confirm the original thesis, which is the opposite of the job."""
        base = symbol.split("-")[0]
        asset = known.get(base) or Asset(base, f"{base}-USDT-SWAP", "", "satellite")
        if "regime" not in state:
            state["regime"], _ = mkt.regime_snapshot(list(listed.values()))
            state["btc_daily"], _ = mkt.candles("BTC-USDT-SWAP", "1D", 300)
            state["hl"] = hyperliquid.fetch_meta_and_contexts()
        return mkt.build_dossier(asset, state["regime"], state["btc_daily"], state["hl"], engine)

    try:
        with run_lock("review", settings.argus_home, timeout_s=SCHEDULED_RUN_WAIT_S):
            with start_run(engine, "review", config_hash=settings.venue) as run_id:
                result = run_review(
                    engine,
                    Router(engine=engine),
                    adapter,
                    run_id,
                    dossier_for,
                    dry_run=dry_run,
                    venue=settings.venue,
                )
                # DECISION_LOGIC §8 rule 6: end every resting entry whose life is over — expired,
                # drifted, or already gone from the venue (F-143, F-144). Inside the lock so two
                # reviews cannot cancel the same order twice. Not optional: a stale entry can fill
                # days after its thesis died, so its failures ride on the exit code.
                from datetime import UTC, datetime

                from argus.config import risk_config
                from argus.execution.resting import sweep

                common = risk_config()["common"]
                try:
                    swept = sweep(
                        engine,
                        adapter,
                        now=datetime.now(UTC),
                        valid_hours=float(common["entry_valid_hours"]),
                        drift_pct=float(common["entry_drift_cancel_pct"]),
                        dry_run=dry_run,
                        venue=settings.venue,
                    )
                    for done in swept.settled:
                        console.print(f"  entry {done.symbol} -> {done.to_state}: {done.reason}")
                    result.errors.extend(f"resting entry: {e}" for e in swept.errors)
                except Exception as exc:  # noqa: BLE001 - reported through the exit code
                    result.errors.append(f"resting-entry sweep failed: {type(exc).__name__}: {exc}")
                if result.errors:
                    # Review J-N3: raised inside `start_run` so the run row says FAILED, not OK.
                    raise ReviewIncompleteError("; ".join(result.errors)[:1900])
    except ReviewIncompleteError:
        pass  # recorded as FAILED; the rest still reports, and the exit code below is 1
    except CodeChangedError as exc:
        _restart_on_new_code(exc)
    except LockBusyError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=0) from exc

    # Post-mortems for every closed trade that has none yet. Due ones are read from the journal,
    # not from what this run noticed: the watchdog marks nearly every close first (F-151).
    # Optional criticality: a missed one costs a lesson, not a trade, so it never blocks the review.
    try:
        from datetime import UTC, datetime

        from argus.journal import positions as positions_journal
        from argus.runs.review import postmortems_due, write_postmortems
        from argus.runs.watchdog import read_halt, resting_through

        closed = positions_journal.newly_closed(
            engine, adapter.fetch_positions(), venue=settings.venue
        )
        if closed and not dry_run:
            try:
                book = adapter.fetch_open_orders()
            except Exception:  # noqa: BLE001 - the close must still be marked
                book = []
            positions_journal.mark_closed(
                engine,
                run_id,
                closed,
                realised=positions_journal.venue_realised(adapter, closed),
                resting=resting_through([c.symbol for c in closed], book),
            )
        # A dry run marks nothing, so what it noticed itself is not in the journal yet.
        due = postmortems_due(engine, datetime.now(UTC), venue=settings.venue) + (
            closed if dry_run else []
        )
        if due and read_halt(settings.halt_file) is not None:
            # HALT means no model is asked (J-N5). Still due once it clears.
            console.print(f"[yellow]post-mortems wait: HALT present ({len(due)} due)[/yellow]")
        elif due:
            verdicts = write_postmortems(
                engine, Router(engine=engine), run_id, due, dry_run=dry_run, venue=settings.venue
            )
            for verdict in verdicts:
                console.print(f"  post-mortem {verdict}")
    except Exception as exc:  # noqa: BLE001 - bookkeeping never fails the run it follows
        console.print(f"[yellow]post-mortems skipped: {type(exc).__name__}: {exc}[/yellow]")

    console.print(f"[bold]{result.summary()}[/bold]")
    table = Table(title="review" + (" (dry run — nothing sent)" if dry_run else ""))
    for column in ("symbol", "action", "applied", "detail"):
        table.add_column(column)
    for outcome in result.outcomes:
        table.add_row(
            outcome.symbol,
            outcome.action,
            "yes" if outcome.applied else "—",
            outcome.detail[:90],
        )
    if result.outcomes:
        console.print(table)
    for error in result.errors:
        console.print(f"[red]{error}[/red]")
    raise typer.Exit(code=1 if result.errors else 0)


@app.command()
def watchdog(once: bool = typer.Option(False, help="Run a single pass and exit")) -> None:
    """No-LLM safety check: verify every position has an exchange-side stop, repair, alert."""
    if not once:
        console.print(
            "[yellow]only --once is implemented; systemd timers drive the cadence[/yellow]"
        )
        raise typer.Exit(code=2)
    from argus.exchange.okx import OKXAdapter
    from argus.journal.db import open_journal, start_run
    from argus.notify.telegram import Notifier
    from argus.runs.lock import WATCHDOG_WAIT_S, CodeChangedError, LockBusyError, run_lock
    from argus.runs.watchdog import Watchdog

    engine = open_journal()
    _telemetry("argus-watchdog", engine)
    home = get_settings().argus_home
    try:
        lock = run_lock("watchdog", home, timeout_s=WATCHDOG_WAIT_S)
        lock.__enter__()
    except CodeChangedError as exc:
        _restart_on_new_code(exc)
    except LockBusyError as exc:
        # A manager or review run is placing orders; repairing on top of it is how a position
        # ends up with two stops. Only a run far past its budget gets here; the next tick is 20
        # minutes away.
        console.print(f"[yellow]{exc} — skipping this tick[/yellow]")
        # ...but the skip is journalled, and a second in a row alerts and fails the unit, so a
        # lock held across ticks is never a silent gap in stop verification (J-4).
        from argus.journal.db import consecutive_skips, record_skipped_run
        from argus.observability import alerts

        record_skipped_run(engine, "watchdog", str(exc))
        skipped = consecutive_skips(engine, "watchdog")
        alerts.dispatch([alerts.watchdog_skipped(skipped)], engine=engine)
        raise typer.Exit(code=SKIPPED_EXIT if skipped >= 2 else 0) from exc
    # Released in `finally` (review X-11): an exception between the hand-entered lock and its
    # exit used to leave release to process death and flock, hiding the leak.
    try:
        # The venue, not a placeholder: equity snapshots hang off this run, and an equity
        # figure nobody can attribute to an account is a figure nobody can trust (F-131).
        with start_run(engine, "watchdog", config_hash=get_settings().venue) as run_id:
            result = Watchdog(
                exchange=OKXAdapter(),
                engine=engine,
                notifier=Notifier.from_settings(),
                # No `plan_stops`: the watchdog reads them from the journal on each pass, against
                # the venue's book, so a resting entry or a closed trade cannot supply the stop
                # (X-6, review 2026-10-07).
            ).run_once(run_id)
    finally:
        lock.__exit__(None, None, None)
    table = Table(title=f"watchdog {run_id[:8]}")
    table.add_column("field")
    table.add_column("value")
    for field, value in (
        ("positions", str(result.positions)),
        ("repaired", ", ".join(result.repaired) or "-"),
        ("repair failures", ", ".join(result.repair_failures) or "-"),
        ("margin warnings", ", ".join(result.margin_warnings) or "-"),
        ("drawdown %", f"{result.drawdown_pct:.2f}" if result.drawdown_pct is not None else "-"),
        ("halted", str(result.halted)),
        ("kill-switch fired", str(result.killed)),
        ("notes", "; ".join(result.notes) or "-"),
    ):
        table.add_row(field, value)
    console.print(table)
    # A venue hiccup with a flat book exits 0 with the reason on screen. Exiting 1 for routine
    # OKX maintenance produced three `Failed` units in three days with nothing open, and an
    # operator who sees that learns to ignore the alert that matters (F-140).
    if not result.ok and result.degraded_not_failed(engine, venue=get_settings().venue):
        console.print(
            "[yellow]degraded: the venue was not reached, and the journal shows nothing "
            "open — no position went unverified[/yellow]"
        )
        raise typer.Exit(code=0)
    raise typer.Exit(code=0 if result.ok else 1)


@app.command()
def reflect(
    window: int = typer.Option(30, help="How many closed trades to reflect over"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the trades and stop"),
) -> None:
    """Weekly: read closed trades, propose lessons, print what code let through.

    Places nothing and reads no market data. The only thing it changes is what the Trader and the
    Analyst are told on later runs, and every candidate has to clear `lessons.py` to change that.
    """
    from argus.journal import lessons as memory
    from argus.journal.db import open_journal, start_run
    from argus.journal.trades import closed_trades
    from argus.llm.router import Router
    from argus.runs.reflect import run_reflection

    engine = open_journal()
    venue = get_settings().venue
    if dry_run:
        trades = closed_trades(engine, limit=window, venue=venue)
        console.print(f"{len(trades)} closed trades in the window")
        for trade in trades:
            verdict = (trade.get("postmortem") or {}).get("process_verdict", "-")
            console.print(
                f"  {trade['trade_id']}  {trade['symbol']:6} {str(trade.get('side')):5}"
                f" {str(trade.get('outcome_r')):>6}R  {verdict:6} {trade.get('exit_reason') or '-'}"
            )
        console.print(f"active lessons:\n{memory.render(memory.active(engine))}")
        return

    _telemetry("argus-reflect", engine)
    # A real run row: lessons and post-mortems carry this run_id, and a bare `new_id()` pointed
    # them at a run nobody could look up — when, in which mode, under which config (J-N8).
    with start_run(engine, "reflect", config_hash=venue) as run_id:
        result = run_reflection(engine, Router(engine=engine), run_id, window=window, venue=venue)
    console.print(f"{result.trades} closed trades, {result.proposed} proposed, {result.kept} kept")
    if result.failed:
        console.print(f"[yellow]reflection skipped: {result.failed}[/yellow]")
    for lesson, reason in result.refused:
        # A refusal is the interesting output: it says what the model tried to talk itself into.
        console.print(f"[yellow]refused: {lesson!r} — {reason}[/yellow]")
    if result.note:
        console.print(f"note: {result.note}")
    table = Table(title=f"active lessons ({result.model})")
    for column in ("scope", "lesson", "support", "against"):
        table.add_column(column)
    for lesson in result.active:
        table.add_row(lesson.scope, lesson.lesson, str(lesson.support), str(lesson.contradiction))
    console.print(table if result.active else "no lessons have cleared the evidence bar yet")
    raise typer.Exit(code=0 if result.ok else 1)


@app.command()
def replay(
    run_id: str = typer.Argument("", help="Run id, or omit with --last"),
    prompt_version: str = typer.Option("", help="Phase 4: re-run stored prompts at this version"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable dump of the whole run"),
    last: bool = typer.Option(False, "--last", help="Replay the most recent manager run"),
) -> None:
    """Reconstruct a finished run from the journal. Reads only — no network, no LLM, no orders."""
    from argus.journal.db import open_journal_readonly
    from argus.journal.replay import RunNotFoundError, as_dict, last_run_id, load

    if prompt_version:
        console.print("[yellow]--prompt-version needs the LLM agents; landing in Phase 4[/yellow]")
        raise typer.Exit(code=2)
    try:
        # Read-only: replay must neither create a journal nor migrate the one it inspects (J-10).
        engine = open_journal_readonly()
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if last:
        run_id = last_run_id(engine) or ""
    if not run_id:
        console.print("[red]give a run id, or --last[/red]")
        raise typer.Exit(code=2)
    try:
        replayed = load(engine, run_id)
    except RunNotFoundError as exc:
        console.print(f"[red]no run {run_id} in the journal[/red]")
        raise typer.Exit(code=1) from exc

    if as_json:
        console.print_json(_json.dumps(as_dict(replayed), default=str))
        return

    run = replayed.run
    console.print(
        f"[bold]{run['kind']}[/bold] {run_id} · {run['status']} · mode={run['mode']}"
        f"{' · shadow' if run['shadow'] else ''} · {run['started_at']} → "
        f"{run['finished_at'] or 'unfinished'}"
    )
    if run.get("error"):
        console.print(f"[red]error:[/red] {run['error']}")
    if replayed.regime:
        regime = replayed.regime
        modifiers = ", ".join(regime.get("modifiers") or []) or "none"
        console.print(
            f"regime [bold]{regime['label']}[/bold] · risk×{regime['risk_mult']}"
            f"{' · limit-only' if regime['limit_only'] else ''} · modifiers: {modifiers}"
        )

    if replayed.dossiers:
        table = Table(title="Dossiers", box=None)
        for column in ("symbol", "source", "tokens", "trade-critical", "gaps"):
            table.add_column(column)
        for dossier in replayed.dossiers:
            table.add_row(
                dossier["symbol"],
                dossier["source"],
                str(dossier["tokens"]),
                "yes" if dossier["trade_critical"] else "NO",
                ", ".join(dossier["gaps"]) or "-",
            )
        console.print(table)

    if replayed.decisions:
        table = Table(title="Decisions", box=None)
        for column in ("symbol", "side", "verdict", "step", "why"):
            table.add_column(column)
        for decision in replayed.decisions:
            why = (
                "; ".join(f"{r['name']}: {r['detail']}" for r in decision.rejections)
                or decision.state
            )
            table.add_row(
                decision.symbol,
                decision.side or "-",
                decision.verdict,
                "-" if decision.step is None else str(decision.step),
                why,
            )
        console.print(table)
    if replayed.llm_calls:
        table = Table(title="LLM calls", box=None)
        for column in ("model", "calls"):
            table.add_column(column)
        for label, count in sorted(replayed.llm_models.items(), key=lambda kv: -kv[1]):
            table.add_row(label, str(count))
        console.print(table)
        console.print(
            "  ".join(f"{status}={count}" for status, count in replayed.llm_calls.items())
        )

    counts = replayed.counts
    console.print(
        f"{len(replayed.dossiers)} dossiers · {len(replayed.decisions)} plans "
        f"({counts['APPROVED']} approved, {counts['ADJUSTED']} adjusted, "
        f"{counts['REJECTED']} rejected)"
    )


@app.command(name="backtest-rules")
def backtest_rules() -> None:
    _not_yet("backtest-rules")


@app.command(name="eval-report")
def eval_report() -> None:
    """The latest golden-set result per model, with the reason for every failure.

    The scheduled eval prints a table truncated to the terminal width; this reads the journal,
    where the full model names and the notes explaining each failure are kept.
    """
    from sqlalchemy import text

    from argus.journal.db import open_journal

    engine = open_journal()
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT provider, model, tier, cases, correct, invented, schema_fail, errors,"
                    " p50_ms, usable, notes, created_at FROM model_evals"
                    " WHERE created_at = (SELECT MAX(created_at) FROM model_evals m2"
                    "   WHERE m2.provider = model_evals.provider AND m2.model = model_evals.model)"
                    " ORDER BY usable DESC, correct DESC, p50_ms"
                )
            )
            .mappings()
            .all()
        )
    if not rows:
        console.print("[yellow]no evals recorded yet[/yellow]")
        return
    for r in rows:
        mark = "[green]yes[/green]" if r["usable"] else "[red]NO[/red]"
        console.print(
            f"{r['provider']}/{r['model']:<44} {r['tier']:<12} {r['correct']}/{r['cases']} "
            f"inv={r['invented']} sch={r['schema_fail']} err={r['errors']} "
            f"p50={r['p50_ms']}ms {mark}"
        )
        notes = _json.loads(r["notes"] or "[]")
        if notes:
            console.print(f"    [dim]{notes[0][:120]}[/dim]")
    console.print(f"[dim]measured {rows[0]['created_at']}[/dim]")


@app.command(name="eval-models")
def eval_models(
    full: bool = typer.Option(
        False, "--full", help="All golden cases instead of the three-case screen"
    ),
    gap_s: float = typer.Option(4.0, help="Seconds between calls — never benchmark in parallel"),
) -> None:
    """Score every configured model against the golden set and record the result.

    Serial with spacing on purpose: running models concurrently makes them rate-limit each other
    and the result says more about the harness than the models (F-095).
    """
    import time as clock

    from argus.journal.db import open_journal
    from argus.llm.golden import CASES, SCREEN_CASES, evaluate_model, record_eval
    from argus.llm.router import tier_refs

    cases = CASES if full else SCREEN_CASES
    engine = open_journal()
    table = Table(title=f"model eval ({len(cases)} cases)")
    for column in ("model", "tier", "correct", "invented", "schema", "errors", "p50", "usable"):
        table.add_column(column)

    seen: set[str] = set()
    failures = 0
    for tier in ("fast", "strong_json", "reasoning"):
        for ref in tier_refs(tier):
            if ref.label in seen:
                continue
            seen.add(ref.label)
            report = evaluate_model(ref, cases)
            record_eval(engine, report, tier=tier)
            failures += 0 if report.usable else 1
            table.add_row(
                ref.label,
                tier,
                f"{report.correct}/{report.total}",
                str(report.invented),
                str(report.schema_fail),
                str(report.errors),
                f"{report.p50_ms}ms",
                "yes" if report.usable else "NO",
            )
            clock.sleep(gap_s)
    console.print(table)
    console.print(f"{len(seen) - failures}/{len(seen)} models usable")


@app.command(name="eval-agents")
def eval_agents(
    agent: str = typer.Option("", help="One role, or empty for every role with a suite"),
    model: str = typer.Option("", help="provider/model to test, or empty for the role's leader"),
    gap_s: float = typer.Option(3.0, help="Seconds between calls"),
) -> None:
    """Score a role's own golden suite against a model — quality per agent, not per tier.

    `eval-models` asks "can this model answer at all"; this asks "is this model any good at this
    particular job", which is the only question that decides the map in `config/models.yaml`.
    """
    import time as clock

    from argus.config import models_config
    from argus.llm.golden import SUITES, evaluate_agent
    from argus.llm.router import agent_policy, tier_refs

    temps = (models_config().get("policy") or {}).get("temperature", {})

    roles = [agent] if agent else list(SUITES)
    table = Table(title="agent quality")
    for column in ("agent", "model", "score", "invented", "schema", "errors", "p50"):
        table.add_column(column)
    for role in roles:
        if role not in SUITES:
            console.print(f"[red]no golden suite for {role}[/red]")
            raise typer.Exit(2)
        policy = agent_policy(role)
        # Resolve by label against the catalogue rather than constructing a ref: the env var and
        # the rate limits come from `config/models.yaml`, and a hand-built ref would lose both.
        pool = [r for tier in ("fast", "strong_json", "reasoning") for r in tier_refs(tier)]
        if model:
            matches = [r for r in pool if r.label == model or r.label.endswith(f"/{model}")]
            if not matches:
                console.print(f"[red]no configured model matching {model!r}[/red]")
                raise typer.Exit(2)
            ref = matches[0]
        else:
            tier = [r for r in tier_refs(policy.tier)]
            ref = next((r for r in tier if r.family == policy.families[0]), tier[0])
        temperature = float(temps.get(role, temps.get("default", 0.3)))
        report = evaluate_agent(ref, role, temperature=temperature)
        table.add_row(
            role,
            ref.label,
            f"{report.correct}/{report.total}",
            str(report.invented),
            str(report.schema_fail),
            str(report.errors),
            f"{report.p50_ms}ms",
        )
        for note in report.notes:
            console.print(f"  [yellow]{role}: {note}[/yellow]")
        clock.sleep(gap_s)
    console.print(table)


@app.command()
def ui(
    port: int = 8787,
    host: str = typer.Option(
        "127.0.0.1", help="Loopback only (127.0.0.1, localhost, ::1); 'auto' means 127.0.0.1."
    ),
    allow_remote: bool = typer.Option(
        False,
        "--allow-remote",
        help="Permit a non-loopback bind. Nothing authenticates the page; prefer tailscale serve.",
    ),
) -> None:
    """Read-only dashboard: runs, decisions, gate reasons, model quality, lessons.

    There are no POST routes and no writes anywhere in `argus/ui/`. A page that cannot place,
    amend or cancel anything is safe to port-forward or leave open; control stays in the CLI and
    in Telegram, where it is authenticated.
    """
    from argus.ui.app import serve

    # A deployed unit outlives the code that wrote it: `update.sh` syncs code, `install-units.sh`
    # syncs units, and between the two the service can pass an argument this binary no longer
    # understands. `--host auto` shipped for exactly one commit and took the dashboard down with
    # "Temporary failure in name resolution" when the option was removed (F-135). Accept it.
    if host == "auto":
        host = "127.0.0.1"
    # One spelling for the check and the bind: `[::1]` is URL syntax, and a socket handed the
    # brackets fails to resolve, so the unit crash-looped on a host the check had approved.
    host = host.strip("[]")

    # Refused, not warned about (P-N10 / P-14). A yellow line scrolls past in journald while the
    # whole journal is served to anyone who can reach the port, and nothing here authenticates.
    # Exposure belongs to `tailscale serve`, which adds TLS and device auth in front of a
    # loopback bind (F-134); a wider bind has to be asked for by name.
    if not _is_loopback(host):
        if not allow_remote:
            console.print(
                f"[red]refusing to bind {host}: nothing authenticates the journal.[/red] Bind "
                "127.0.0.1 and expose it with `tailscale serve`, or pass --allow-remote if you "
                "really mean to serve it beyond this machine."
            )
            raise typer.Exit(2)
        console.print(
            "[yellow]--allow-remote: binding beyond localhost — nothing authenticates the "
            "journal[/yellow]"
        )

    url_host = f"[{host}]" if ":" in host else host
    console.print(f"[green]ARGUS[/green] http://{url_host}:{port} — read-only, ctrl-c to stop")
    serve(port=port, host=host)


def _is_loopback(host: str) -> bool:
    """True only for an address that cannot be reached from another machine.

    `localhost` is the one name accepted: any other hostname would need resolving to know, and a
    name that resolves somewhere wider today is exactly the bind this check exists to stop.
    """
    import ipaddress

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _requested_symbols(symbols: str) -> list[str]:
    """`--symbols`, minus anything blocklisted or an excluded kind (D-15).

    A symbol missing from core and whitelist becomes a bare satellite Asset, so without this a
    blocked one passed by hand was researched, proposed and placeable.
    """
    from argus.features.universe import refused

    # Once each, in the order given: a repeated symbol journalled a dossier and a plan per
    # repetition, all for one asset (J-12).
    chosen = list(dict.fromkeys(s.strip().upper() for s in symbols.split(",") if s.strip()))
    dropped = [s for s in chosen if refused(s)]
    if dropped:
        console.print(f"[yellow]blocklisted or excluded, not researched: {', '.join(dropped)}[/]")
    kept = [s for s in chosen if s not in dropped]
    if chosen and not kept:
        console.print("[red]Every requested symbol is refused; nothing to research.[/red]")
        raise typer.Exit(code=2)
    return kept


def _known_assets() -> dict[str, Any]:
    """Every core and whitelist symbol with its metadata, blocked ones included.

    For review only. Blocking a symbol stops new entries; a position already open on it still
    needs its coingecko, defillama and unlock families to be managed (D-15).
    """
    from argus.features.universe import core_assets, whitelist_assets

    whitelist, _ = whitelist_assets(include_blocked=True)
    return {a.symbol: a for a in [*core_assets(include_blocked=True), *whitelist]}


def _listed_assets() -> list[Any]:
    from argus.features.universe import core_assets, whitelist_assets

    whitelist, expired = whitelist_assets()
    for symbol in expired:
        console.print(
            f"[yellow]whitelist entry expired: {symbol} (renew in whitelist.yaml)[/yellow]"
        )
    return [*core_assets(), *whitelist]


@app.command()
def regime() -> None:
    """Regime snapshot: label, risk multipliers, limit-only flag, inputs and gaps."""
    from argus.features.market import regime_snapshot

    view, prov = regime_snapshot(_listed_assets(), limits=get_settings().limits)
    colour = {"TREND_UP": "green", "TREND_DOWN": "red", "CHOP": "yellow", "EVENT_RISK": "magenta"}
    console.print(
        f"[bold {colour[view.label]}]{view.label}[/] risk×{view.risk_mult:.2f} "
        f"(longs ×{view.risk_mult_long:.2f}, "
        f"BTC/ETH longs ×{view.risk_mult_for('BTC', 'long'):.2f}, "
        f"shorts ×{view.risk_mult_short:.2f}) limit_only={view.limit_only}"
    )
    for reason in view.reasons:
        console.print(f"  reason: {reason}")
    for modifier in view.modifiers:
        console.print(f"  modifier: {modifier}")
    table = Table(title="regime inputs")
    table.add_column("input")
    table.add_column("value")
    assert view.inputs is not None
    for name, value in view.inputs.model_dump().items():
        shown = (
            "[red]—[/red]"
            if value is None
            else (f"{value:,.4f}" if isinstance(value, float) else str(value))
        )
        table.add_row(name, shown)
    console.print(table)
    for gap in view.data_gaps:
        console.print(f"[red]gap[/red] {gap}")
    for obs in prov:
        if obs.quality != "ok":
            console.print(
                f"[red]source[/red] {obs.source}: {obs.notes[0] if obs.notes else obs.quality}"
            )


@app.command()
def universe() -> None:
    """Today's research set: core + active whitelist + ≤3 screened satellites."""
    from datetime import UTC, datetime

    from argus.config import universe_config
    from argus.data.fetchers import okx_public
    from argus.data.http import fetch_json
    from argus.features import universe as uni

    cfg = universe_config()
    listed = _listed_assets()
    host = get_settings().okx_public_host.rstrip("/")
    inst = fetch_json(
        "okx.public.instruments",
        f"{host}/api/v5/public/instruments",
        1,
        "reference",
        params={"instType": "SWAP"},
    )
    ticks = okx_public.fetch_tickers()
    rows_i = inst.data.get("data", []) if isinstance(inst.data, dict) else []
    rows_t = ticks.data.get("data", []) if isinstance(ticks.data, dict) else []
    screened = uni.screen(
        rows_i,
        rows_t,
        now=datetime.now(UTC),
        blocklist=uni.blocklist(),
        already_listed={a.symbol for a in listed},
    )
    from argus.features.market import candles

    for row in screened:
        daily, _ = candles(row.okx, "1D", 30)
        funding_obs = okx_public.fetch_funding(row.okx)
        rate = None
        if isinstance(funding_obs.data, dict) and funding_obs.data.get("data"):
            raw = funding_obs.data["data"][0].get("fundingRate")
            rate = float(raw) if raw not in (None, "") else None
        uni.enrich(row, daily, rate)
    screened, manic = uni.drop_mania(screened)
    for symbol, why in manic:
        console.print(f"[yellow]mania guard[/yellow] {symbol}: {why}")
    ranked = uni.rank_by_attention(screened)
    chosen = uni.select(
        [a for a in listed if a.source == "core"],
        [a for a in listed if a.source == "whitelist"],
        ranked,
        max_satellites=int(cfg["satellites"]["max"]),
        max_dossiers=int(cfg["max_dossiers_per_run"]),
    )
    table = Table(title=f"research set ({len(chosen)}/{cfg['max_dossiers_per_run']})")
    for column in ("symbol", "source", "note"):
        table.add_column(column)
    scores = {r.symbol: r for r in ranked}
    for asset in chosen:
        note = ""
        if asset.expires:
            note = f"expires {asset.expires.isoformat()}"
        elif asset.symbol in scores:
            row = scores[asset.symbol]
            r24 = f"{row.ret_24h:+.0f}%" if row.ret_24h is not None else "n/a"
            r7 = f"{row.ret_7d:+.0f}%" if row.ret_7d is not None else "n/a"
            rv = f"{row.rel_volume:.1f}x" if row.rel_volume is not None else "n/a"
            note = (
                f"attn {row.attention or 0:+.2f} · 24h {r24} · 7d {r7} · vol {rv} · "
                f"${row.turnover_24h / 1e6:,.0f}M"
            )
        table.add_row(asset.symbol, asset.source, note)
    console.print(table)
    console.print(f"screen: {len(screened)} crypto perps passed out of {len(rows_i)} instruments")


@app.command()
def dossier(
    symbol: str,
    print_: bool = typer.Option(False, "--print", help="Pretty-print the rendered dossier"),
    as_json: bool = typer.Option(False, "--json", help="Emit the exact JSON the LLM will see"),
) -> None:
    """Build the full dossier for one asset (technical, derivatives, on-chain, news, catalysts)."""
    import json as _json

    from argus.data.fetchers import hyperliquid
    from argus.features import dossier as ds
    from argus.features.market import build_dossier, candles, regime_snapshot
    from argus.features.universe import Asset
    from argus.journal.db import open_journal

    listed = {a.symbol: a for a in _listed_assets()}
    asset = listed.get(symbol.upper()) or Asset(
        symbol.upper(), f"{symbol.upper()}-USDT-SWAP", "", "satellite"
    )
    view, _ = regime_snapshot(list(listed.values()), limits=get_settings().limits)
    btc_daily, _ = candles("BTC-USDT-SWAP", "1D", 300)
    built = build_dossier(
        asset, view, btc_daily, hyperliquid.fetch_meta_and_contexts(), open_journal()
    )
    text = ds.render(built)
    tokens = ds.token_count(text)
    if as_json:
        print(text)
    elif print_:
        console.print_json(text)
    over = tokens > ds.TOKEN_BUDGET
    console.print(
        f"{built.symbol} ({built.source}): ~{tokens} tokens "
        f"({'[red]over' if over else '[green]within'}[/] {ds.TOKEN_BUDGET}), "
        f"regime {view.label}, trade-critical families "
        f"{'[green]present' if built.trade_critical_present else '[red]missing'}[/], "
        f"{len(built.data_gaps)} gaps, {len(_json.loads(text).get('news', []))} headlines"
    )
    raise typer.Exit(code=0 if built.trade_critical_present and not over else 1)


# ── Operator control plane: halt | resume | flatten (review 2026-09-18 P-2) ─────────────────────
#
# Every incident procedure in HUMAN_GUIDE and RUNBOOK began with `/halt` or `/close all`, and
# neither existed — the Telegram router has no caller. These are the same verbs from a shell on the
# box, built on the pieces the watchdog already trusts: the HALT file (every run checks it before
# placing anything) and the kill-switch flatten (cancel resting orders, then close).

# How long `flatten` waits for a run that holds the lock. A manager only checks HALT when it
# starts, so closing underneath one mid-placement is how a fresh entry would slip past.
FLATTEN_LOCK_WAIT_S = 300.0


@app.command()
def halt(reason: str = typer.Argument(..., help="Why — written into the HALT file")) -> None:
    """Stop every run from opening or changing anything. Open positions keep their exchange-side
    stops, and the watchdog keeps checking them."""
    from argus.runs.watchdog import KILL_MARKER, read_halt, write_halt

    path = get_settings().halt_file
    existing = read_halt(path)
    if existing and KILL_MARKER in existing:
        # Overwriting would drop the marker that makes the watchdog retry an unfinished
        # kill-switch flatten (review X-4). Already halted, and more firmly than this would.
        console.print(f"[yellow]already halted by the kill-switch, kept as is:[/yellow] {existing}")
        raise typer.Exit(code=0)
    write_halt(path, f"operator: {reason}")
    console.print(
        f"[red]HALTED[/red] {path} — no manager places and no review closes, trims or amends "
        "until `resume`. The watchdog still repairs missing stops, and the review still cancels "
        "expired resting entries."
    )


@app.command()
def resume(
    after_kill_switch: bool = typer.Option(
        False,
        "--after-kill-switch",
        help="Required when the kill-switch wrote the HALT: you have read the post-mortem",
    ),
) -> None:
    """Remove the HALT file; the next timer tick may trade again."""
    from argus.runs.watchdog import KILL_MARKER, clear_halt, read_halt

    path = get_settings().halt_file
    existing = read_halt(path)
    if existing is None:
        console.print("not halted — nothing to resume")
        raise typer.Exit(code=0)
    if KILL_MARKER in existing and not after_kill_switch:
        console.print(
            f"[red]the kill-switch wrote this HALT:[/red] {existing}\n"
            "Resume only after the post-mortem is in FAILURE_LOG.md and OKX shows no "
            "position you did not expect. Then: argus resume --after-kill-switch"
        )
        raise typer.Exit(code=2)
    clear_halt(path)
    console.print(f"[green]resumed[/green] (was: {existing}) — the next timer tick may trade")


@app.command()
def flatten(
    symbol: str = typer.Option(
        "", "--symbol", help="One instrument, e.g. BTC-USDT-SWAP. Default: every position"
    ),
    yes: bool = typer.Option(False, "--yes", help="Required. Closes at market, on the account"),
) -> None:
    """Emergency exit: cancel resting orders and close positions at market.

    Without --symbol it first writes a kill-switch HALT, so nothing reopens and every watchdog
    tick retries a close that fails here. It reuses the watchdog's own flatten — the path the
    drawdown kill-switch takes — under the run lock, and journals itself as a `flatten` run.
    """
    from argus.exchange.okx import OKXAdapter
    from argus.exchange.reconcile import asset_of
    from argus.journal.db import open_journal, start_run
    from argus.notify.telegram import Notifier
    from argus.runs.lock import CodeChangedError, LockBusyError, run_lock
    from argus.runs.watchdog import KILL_MARKER, Watchdog, WatchdogResult, read_halt, write_halt

    settings = get_settings()
    scope = symbol or "EVERY position"
    if not yes:
        console.print(
            f"would cancel resting orders and close {scope} at market on the "
            f"{settings.venue.upper()} account"
            + ("" if symbol else ", and write a HALT")
            + ". Add --yes to do it."
        )
        raise typer.Exit(code=2)

    if not symbol:
        existing = read_halt(settings.halt_file) or ""
        if KILL_MARKER not in existing:
            write_halt(settings.halt_file, f"{KILL_MARKER} operator flatten")
        console.print(f"[red]HALTED[/red] {settings.halt_file}")

    engine = open_journal()
    _telemetry("argus-flatten", engine)
    notifier = Notifier.from_settings()
    home = settings.argus_home
    try:
        try:
            lock = run_lock("flatten", home, timeout_s=0)
            lock.__enter__()
        except CodeChangedError:
            raise
        except LockBusyError as busy:
            console.print(f"[yellow]{busy} — waiting up to {FLATTEN_LOCK_WAIT_S:.0f}s[/yellow]")
            lock = run_lock("flatten", home, timeout_s=FLATTEN_LOCK_WAIT_S)
            lock.__enter__()
    except CodeChangedError as exc:
        _restart_on_new_code(exc)
    except LockBusyError as exc:
        console.print(
            f"[red]{exc}[/red] — nothing closed yet. "
            + (
                "The HALT is down, so the watchdog closes everything on its next tick."
                if not symbol
                else "Run this again when that run has finished."
            )
        )
        raise typer.Exit(code=1) from exc
    try:
        with start_run(engine, "flatten", config_hash=settings.venue) as run_id:
            exchange = OKXAdapter()
            positions = exchange.reconcile()["positions"]
            if symbol:
                # The venue reports ccxt's `BTC/USDT:USDT`; the operator types `BTC-USDT-SWAP`.
                # Comparing the raw strings closed nothing and reported success.
                positions = [p for p in positions if asset_of(p["symbol"]) == asset_of(symbol)]
            result = WatchdogResult(run_id=run_id, positions=len(positions))
            watchdog = Watchdog(exchange=exchange, engine=engine, notifier=notifier)
            # The kill-switch's own flatten: cancel every resting order first — on symbols with
            # no position too (X-4, X-N4) — then close.
            watchdog._flatten(run_id, result, positions, asset=asset_of(symbol) if symbol else None)
            if symbol and not positions and not result.cancelled:
                console.print(
                    f"[red]no open position or resting order on {symbol}[/red] on the "
                    f"{settings.venue.upper()} account — nothing closed"
                )
                raise typer.Exit(code=1)
    finally:
        lock.__exit__(None, None, None)

    closed = [p["symbol"] for p in positions if p["symbol"] not in result.close_failures]
    notifier.send(
        f"operator flatten on {settings.venue}: closed {', '.join(closed) or 'nothing'}"
        + (f"; FAILED {', '.join(result.close_failures)}" if result.close_failures else "")
    )
    console.print(f"cancelled resting orders on: {', '.join(result.cancelled) or '-'}")
    console.print(f"closed: {', '.join(closed) or '-'}")
    if result.close_failures:
        console.print(
            f"[red]FAILED to close: {', '.join(result.close_failures)}[/red] — "
            + ("the watchdog retries each tick; " if not symbol else "")
            + "close it in the OKX app if it persists"
        )
        raise typer.Exit(code=1)


# How long a manual trade queues for the run lock. A scheduled manager or review can hold it for a
# few minutes; this is operator-driven, so it waits that long rather than skipping.
MANUAL_LOCK_WAIT_S = 300.0


def _say(message: str) -> None:
    # soft_wrap: rich would otherwise break `--stop-pct` or a command line at a hyphen.
    console.print(message, soft_wrap=True)


@app.command(name="manual-trade")
def manual_trade(
    symbol: str = typer.Option(..., "--symbol", help="Base asset, e.g. BTC"),
    side: str = typer.Option(..., "--side", help="long or short"),
    stop_pct: str = typer.Option(..., "--stop-pct", help="Stop distance from the mark, percent"),
    target_pct: str = typer.Option(..., "--target-pct", help="Target distance, percent"),
    yes: bool = typer.Option(False, "--yes", help="Place it. Without this, only print the order"),
) -> None:
    """ONE manual DEMO trade: minimum size, market entry, isolated 2x, stop attached.

    CLAUDE.md #1 allows a manual trade only when owner asks for it in the session, on demo unless he
    writes "live". This command is the demo half and refuses anything else. It builds the order
    from the venue's own minimum size, prints it, and with --yes places it through the same
    `place_bracket` path the manager uses, journalled as a `manual` run so the watchdog, reconcile
    and the Reviewer treat the position as owned.
    """
    from decimal import Decimal, InvalidOperation

    from argus.execution.place import PlacementError
    from argus.journal.db import open_journal, start_run
    from argus.runs import manual
    from argus.runs.lock import CodeChangedError, LockBusyError, run_lock

    settings = get_settings()
    why = manual.refusal(settings)
    if why:
        _say(f"[red]{why}[/red]")
        raise typer.Exit(code=2)
    try:
        base = manual.base_of(symbol)
        picked = side.strip().lower()
        if picked not in ("long", "short"):
            raise manual.ManualTradeRefusedError(f"--side must be long or short, got {side!r}")
        try:
            stop, target = Decimal(stop_pct), Decimal(target_pct)
        except InvalidOperation:
            raise manual.ManualTradeRefusedError(
                "--stop-pct and --target-pct must be numbers"
            ) from None
        if not (stop.is_finite() and target.is_finite()):
            raise manual.ManualTradeRefusedError("--stop-pct and --target-pct must be finite")
        manual.check_levels(stop, target)
    except manual.ManualTradeRefusedError as exc:
        _say(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    from argus.exchange.errors import venue_error
    from argus.exchange.okx import OKXAdapter

    try:
        adapter = OKXAdapter()
        ticket = manual.build_ticket(adapter, base, picked, stop, target)  # type: ignore[arg-type]
    except manual.ManualTradeRefusedError as exc:
        _say(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001 - say what failed, place nothing
        _say(f"[red]could not read the venue: {venue_error(exc)}[/red]")
        raise typer.Exit(code=1) from exc

    # Demo only: refusal() has already ruled the live account out.
    _say(f"[yellow]MANUAL TRADE · {settings.venue.upper()} account[/yellow] mode={settings.mode}")
    for line in ticket.lines(settings.venue):
        _say(f"  {line}")
    if not yes:
        try:
            pending = manual.blocker(adapter, settings, ticket)
        except Exception as exc:  # noqa: BLE001 - a preview is still useful without it
            pending = f"could not check the venue's book: {venue_error(exc)}"
        if pending:
            _say(f"[yellow]--yes would be refused right now: {pending}[/yellow]")
        _say("Nothing placed. Add --yes to place exactly this order.")
        raise typer.Exit(code=0)

    engine = open_journal()
    _telemetry("argus-manual-trade", engine)
    try:
        with run_lock("manual", settings.argus_home, timeout_s=MANUAL_LOCK_WAIT_S):
            blocked = manual.blocker(adapter, settings, ticket)
            if blocked:
                _say(f"[red]not placed: {blocked}[/red]")
                raise typer.Exit(code=1)
            with start_run(engine, "manual", config_hash=settings.venue) as run_id:
                plan_id, order_id = manual.place(engine, adapter, settings, run_id, ticket)
    except CodeChangedError as exc:
        _restart_on_new_code(exc)
    except LockBusyError as exc:
        _say(f"[red]{exc}[/red] - nothing placed. Run it again when that run has finished.")
        raise typer.Exit(code=1) from exc
    except PlacementError as exc:
        _say(f"[red]NOT PLACED - the venue refused it: {exc}[/red]")
        _say("No position was opened; the order is journalled REJECTED (or NEW if unknown).")
        raise typer.Exit(code=1) from exc

    with engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT cl_ord_id, exchange_id FROM orders WHERE order_id = :o"),
                {"o": order_id},
            )
            .mappings()
            .one()
        )
    _say(
        f"[green]PLACED[/green] run {run_id[:8]} plan {plan_id[:8]} order {order_id[:8]} "
        f"clOrdId {row['cl_ord_id']} venue id {row['exchange_id']}"
    )
    try:
        protection = manual.protection_after(adapter, ticket)
    except Exception as exc:  # noqa: BLE001 - placed already; report what could not be checked
        protection = f"unchecked ({venue_error(exc)})"
    if protection == "protected":
        _say(f"[green]stop resting at the venue[/green] at {ticket.stop}")
    elif protection == "pending":
        _say("position not on the venue's book yet; the watchdog's next tick will confirm its stop")
    else:
        _say(
            f"[red]stop not confirmed ({protection})[/red] - run `argus watchdog --once`; it "
            "repairs a position that has none, and the OKX app shows it"
        )
    _say("To close it (this one instrument only - without --symbol flatten writes a HALT):")
    _say(f"  {manual.close_command(base)}")
    _say(f"  on the box: sudo -u argus /opt/argus-project/.venv/bin/{manual.close_command(base)}")
    if protection.startswith(("unprotected", "unchecked")):
        raise typer.Exit(code=1)


@app.command()
def audit() -> None:
    """Verify every ledger hash chain and its insert-only guards. Exit 1 on any break (J-3).

    Read-only apart from applying pending migrations. Copy the printed head hashes and row counts
    off the box now and then: a chain rewritten end to end verifies on its own, but it cannot
    reproduce a head hash recorded somewhere it could not reach.
    """
    from argus.journal import ledger
    from argus.journal.db import open_journal

    report = ledger.audit(open_journal())
    table = Table(title="ledger audit")
    for column in ("table", "rows", "chain", "guards", "head"):
        table.add_column(column)
    for entry in report:
        chain = "[green]ok[/green]" if entry.chain_ok else f"[red]BROKEN at {entry.broken_id}[/red]"
        if entry.chain_ok and entry.empty:
            chain = "[yellow]EMPTY[/yellow]"
        faults = [f"missing {g}" for g in entry.missing_guards] + [
            f"altered {g}" for g in entry.altered_guards
        ]
        guards = "[green]ok[/green]" if not faults else f"[red]{', '.join(faults)}[/red]"
        table.add_row(entry.table, str(entry.rows), chain, guards, entry.head[:16])
    console.print(table)
    raise typer.Exit(code=0 if all(entry.ok for entry in report) else 1)


if __name__ == "__main__":
    app()
