"""A read-only dashboard over the journal.

There are no POST routes and no writes anywhere in this package. That is deliberate: a page that
cannot place, amend or cancel anything is safe to leave open, to port-forward during a demo, or to
show to someone standing behind you. Control stays in the CLI and in Telegram, where it is
authenticated.

One journal, two accounts. The header's Demo | Live switch chooses which account's rows every
panel shows. It is a link (`?venue=demo|live`) remembered in a cookie, not a form: choosing what to
LOOK at changes nothing on the server, and the band under the header keeps it separate from what
the system TRADES ("Viewing LIVE · armed on DEMO").

It binds 127.0.0.1 by default (ARCHITECTURE.md). Reach it from a laptop over SSH:

    ssh -N -L 8787:127.0.0.1:8787 operator@<vps>
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from argus.config import get_settings
from argus.journal.db import open_journal_readonly, schema_is_current
from argus.ui import pnl, queries

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

VENUE_COOKIE = "argus_venue"
VENUE_COOKIE_MAX_AGE = 365 * 24 * 3600


def _viewing(request: Request) -> tuple[str, bool]:
    """Which account to show, and whether this request chose it.

    Query string, then cookie, then the venue the config is armed on. Anything that is not exactly
    `demo` or `live` is ignored rather than echoed — the value ends up in links and in a cookie.
    """
    asked = request.query_params.get("venue")
    if asked is not None and asked in queries.VENUES:
        return asked, True
    remembered = request.cookies.get(VENUE_COOKIE)
    if remembered is not None and remembered in queries.VENUES:
        return remembered, False
    return get_settings().venue, False


def _ago(value: str | None) -> str:
    from datetime import UTC, datetime

    if not value:
        return "-"
    try:
        then = datetime.fromisoformat(value)
    except ValueError:
        return value
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - then).total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def create_app() -> FastAPI:
    app = FastAPI(title="ARGUS", docs_url=None, redoc_url=None)
    TEMPLATES.env.filters["ago"] = _ago
    # The dashboard is read-only (review P1-6): it opens the journal without creating or migrating
    # it, and refuses to serve a schema the code has moved past rather than quietly altering it.
    engine = open_journal_readonly()
    if not schema_is_current(engine):
        raise RuntimeError(
            "journal schema is behind this code: run `argus doctor` or any argus run once to "
            "apply migrations, then start the dashboard again"
        )

    def page(
        request: Request, template: str, viewing: str, chosen: bool, **ctx: Any
    ) -> HTMLResponse:
        # Staleness goes on every page, not just the one someone thought to open. The gap is
        # computed here rather than left to the reader's arithmetic (F-141). Not scoped by the
        # venue switch: it asks whether the configured account's manager is running, the same
        # read as the watchdog's alert, whichever account is being viewed (F-150).
        settings = get_settings()
        ctx.setdefault(
            "staleness",
            queries.staleness(engine, settings.manager_max_gap_hours, venue=settings.venue),
        )
        # Global for the same reason: a position without a stop is announced whichever account
        # is being viewed.
        ctx.setdefault("unprotected", queries.unprotected_positions(engine))
        ctx.setdefault("switch_path", request.url.path)
        response = TEMPLATES.TemplateResponse(request, template, {"viewing": viewing, **ctx})
        if chosen:
            # A view preference held by the browser, not state on the server: the page is still
            # GET-only and still cannot change anything about either account.
            response.set_cookie(
                VENUE_COOKIE,
                viewing,
                max_age=VENUE_COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
            )
        return response

    @app.get("/", response_class=HTMLResponse)
    def trading(request: Request) -> HTMLResponse:
        """The trading view: what the system did with money, and why it refused when it did not."""
        viewing, chosen = _viewing(request)
        return page(
            request,
            "trading.html",
            viewing,
            chosen,
            nav="trading",
            venue=queries.venue_state(engine),
            equity=queries.equity(engine, venue=viewing),
            kill_switch=queries.kill_switch(engine),
            cadence=queries.run_cadence(engine, venue=viewing),
            orders=queries.orders(engine, venue=viewing),
            open_positions=queries.open_positions(engine, venue=viewing),
            postmortems=queries.postmortems(engine, venue=viewing),
            decisions=queries.decisions(engine, venue=viewing),
            histogram=queries.refusal_histogram(engine, venue=viewing),
            runs=queries.recent_runs(engine, limit=12, venue=viewing),
            unattributed=queries.unattributed(engine),
        )

    @app.get("/pnl", response_class=HTMLResponse)
    def pnl_view(request: Request) -> HTMLResponse:
        """The performance view: realised and unrealised PnL in $ and %, per account."""
        viewing, chosen = _viewing(request)
        return page(
            request,
            "pnl.html",
            viewing,
            chosen,
            nav="pnl",
            venue=queries.venue_state(engine),
            pnl=pnl.pnl_panel(engine, viewing),
            min_days=pnl.SHARPE_MIN_DAYS,
            unattributed=queries.unattributed(engine),
        )

    @app.get("/agents", response_class=HTMLResponse)
    def agents(request: Request) -> HTMLResponse:
        """The AI view: which model holds which role, how each was measured, what was learned."""
        viewing, chosen = _viewing(request)
        return page(
            request,
            "agents.html",
            viewing,
            chosen,
            nav="agents",
            venue=queries.venue_state(engine),
            agents=queries.agent_map(),
            lessons=queries.active_lessons(engine),
            scores=queries.agent_scores(engine),
            runs=queries.recent_runs(engine, limit=8, venue=viewing),
        )

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    def run(request: Request, run_id: str) -> HTMLResponse:
        # One run belongs to one account whatever is being viewed, so the switch here goes back
        # to the overview of the account chosen rather than re-rendering the same run.
        viewing, chosen = _viewing(request)
        return page(
            request,
            "run.html",
            viewing,
            chosen,
            nav="trading",
            switch_path="/",
            venue=queries.venue_state(engine),
            run_id=run_id,
            **queries.run_detail(engine, run_id),
        )

    return app


def serve(port: int = 8787, host: str = "127.0.0.1") -> None:
    """Localhost only, and it stays that way.

    An earlier version asked `tailscale ip` and bound the tailnet address directly. That was the
    wrong layer: it silently fell back to localhost when the `argus` user could not reach the
    tailscaled socket, so the service reported healthy while being unreachable from the tailnet
    (F-134). Exposure now belongs to `tailscale serve`, which terminates TLS, applies the
    tailnet's own device authentication, and needs nothing from this process.
    """
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
