"""The dashboard is read-only by construction, and that is the property worth testing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.journal.db import new_id, open_journal
from argus.ui import queries

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.fixture
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from argus.ui import app as app_module

    monkeypatch.setattr(app_module, "open_journal_readonly", lambda *a, **k: engine)
    return TestClient(app_module.create_app())


def _run(engine: Engine, *, shadow: int = 1, mode: str = "demo", venue: str = "demo") -> str:
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r,'manager',:m,:t,'OK',:s,:v)"
            ),
            {"r": run_id, "m": mode, "t": NOW.isoformat(), "s": shadow, "v": venue},
        )
        plan_id = new_id()
        conn.execute(
            text(
                "INSERT INTO plans (plan_id, run_id, created_at, mode, symbol, side, state,"
                " payload, prev_hash, row_hash) VALUES (:p,:r,:t,:m,'SOL','long','PROPOSED',"
                " :payload,'','')"
            ),
            {"p": plan_id, "r": run_id, "t": NOW.isoformat(), "m": mode, "payload": "{}"},
        )
        conn.execute(
            text(
                "INSERT INTO gate_results (gate_result_id, plan_id, run_id, created_at, verdict,"
                " reasons, prev_hash, row_hash) VALUES (:g,:p,:r,:t,'REJECTED',:j,'','')"
            ),
            {
                "g": new_id(),
                "p": plan_id,
                "r": run_id,
                "t": NOW.isoformat(),
                "j": json.dumps(
                    {
                        "reasons": [
                            {
                                "gate": 3,
                                "name": "structure",
                                "verdict": "REJECT",
                                "detail": "stop 104.2 is inside structure",
                            }
                        ]
                    }
                ),
            },
        )
    return run_id


def test_nothing_on_the_dashboard_can_change_anything(client: TestClient) -> None:
    """No POST, PUT, PATCH or DELETE anywhere.

    This is why the page is safe to port-forward or leave open on a laptop during a demo: it
    cannot place, amend or cancel an order. Control lives in the CLI and Telegram, behind auth.
    """
    mutating = [
        route.path
        for route in client.app.routes  # type: ignore[attr-defined]
        if hasattr(route, "methods") and route.methods - {"GET", "HEAD"}
    ]
    assert mutating == []


def test_both_pages_render_with_an_empty_journal(client: TestClient) -> None:
    """A fresh box has no runs, no orders, no evals and no lessons. Both pages must say so."""
    trading = client.get("/")
    assert trading.status_code == 200
    assert "No runs yet" in trading.text
    assert "No orders placed yet" in trading.text

    agents = client.get("/agents")
    assert agents.status_code == 200
    assert "cleared the evidence bar" in agents.text


def test_an_empty_positions_panel_means_flat_not_blind(client: TestClient) -> None:
    """The table is instrumented now, so an empty one is a fact rather than an absence.

    While `positions_snapshots` had no writer this panel said so in as many words, because a zero
    from an uninstrumented source is a guess wearing a number's clothes — and the thing it
    guesses is that nothing is at risk (F-132).
    """
    body = client.get("/").text
    assert "Flat." in body
    assert "Not instrumented" not in body


def test_an_unprotected_position_is_impossible_to_miss(client: TestClient, engine: Engine) -> None:
    """The one state the watchdog exists to prevent has to shout on the page."""
    from argus.journal import positions as journal

    run_id = _run(engine)
    journal.record(
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
            },
            {
                "symbol": "ETH",
                "side": "long",
                "size": "1",
                "entry_price": 50.0,
                "mark_price": 51.0,
                "leverage": 2,
                "unrealised": "1",
            },
        ],
        unprotected={"SOL"},
    )
    body = client.get("/").text
    assert "NO STOP" in body
    assert "stop attached" in body
    assert "Flat." not in body


def test_a_plan_refused_before_sizing_shows_no_size(client: TestClient, engine: Engine) -> None:
    """Zeros there would read as a decision somebody made."""
    _run(engine)
    body = client.get("/").text
    assert "not reached — refused before sizing" in body


def test_the_band_warns_when_the_journal_and_the_config_disagree(
    client: TestClient, engine: Engine
) -> None:
    """The page's config is cached at process start; the run row was written by the process that
    actually traded. When they differ the run is the truth, and the band must say so."""
    _run(engine, shadow=0, mode="auto", venue="live")
    body = client.get("/").text
    assert "DISAGREEMENT" in body
    assert "Trust the run, not this page" in body


def test_an_unrecorded_venue_is_not_called_a_disagreement(
    client: TestClient, engine: Engine
) -> None:
    """Runs written before the venue was journalled carry `-`.

    "I do not know" is not "these conflict". Calling it a disagreement would cry wolf on every
    historical row and teach the reader to ignore the one band that matters (F-131).
    """
    _run(engine, venue="-")
    body = client.get("/").text
    assert "DISAGREEMENT" not in body
    assert "venue not recorded" in body


def test_a_rejected_plan_shows_the_gate_and_the_reason(client: TestClient, engine: Engine) -> None:
    run_id = _run(engine)
    body = client.get(f"/run/{run_id}").text
    assert "rejected" in body
    # The reason is the point: a demo of a system that refuses trades is only interesting if it
    # says which rule refused and why.
    assert "gate 3" in body and "structure" in body
    assert "stop 104.2 is inside structure" in body


def test_a_shadow_run_is_never_described_as_live(client: TestClient, engine: Engine) -> None:
    """The one mislabelling here that could change what someone believes about real money."""
    shadow = client.get(f"/run/{_run(engine, shadow=1, mode='demo')}").text
    assert "places nothing" in shadow or "nothing placed" in shadow
    assert "live execution" not in shadow

    # `demo` without the shadow flag is still not live execution.
    demo = client.get(f"/run/{_run(engine, shadow=0, mode='demo')}").text
    assert "live execution" not in demo

    # Armed is not live either: an armed run on the demo account (F-123) is a rehearsal.
    armed_demo = client.get(f"/run/{_run(engine, shadow=0, mode='auto', venue='demo')}").text
    assert "live execution" not in armed_demo

    live = client.get(f"/run/{_run(engine, shadow=0, mode='auto', venue='live')}").text
    assert "live execution" in live


def test_an_unknown_run_is_handled(client: TestClient) -> None:
    response = client.get("/run/does-not-exist")
    assert response.status_code == 200
    assert "No such run" in response.text


def test_the_agent_map_comes_from_config_not_a_copy(engine: Engine) -> None:
    """Shown from `models.yaml` through the router, so the page cannot drift from what runs."""
    from argus.llm.router import agent_policy

    rows = {row["agent"]: row for row in queries.agent_map()}
    assert len(rows) == 10
    assert rows["trader"]["leader"].startswith("gemini/")
    assert rows["reviewer"]["judges"] == agent_policy("reviewer").counterpart
    assert rows["redteam"]["criticality"] == "safety"


def test_orders_show_their_derived_state_not_the_stored_one(
    client: TestClient, engine: Engine
) -> None:
    """`orders.state` says NEW forever; the truth lives in `position_events`.

    A dashboard that quietly disagrees with the ledger is worse than no dashboard, so this pins
    the join rather than the convenient column.
    """
    from argus.execution.lifecycle import record_order, transition_order

    run_id = _run(engine)
    order = {
        "cl_ord_id": "cl-1",
        "symbol": "SOL",
        "side": "buy",
        "order_type": "limit",
        "price": "100",
        "size": "3",
    }
    with engine.connect() as conn:
        plan_id = conn.execute(text("SELECT plan_id FROM plans LIMIT 1")).scalar()
    order_id = record_order(engine, run_id, order, plan_id=plan_id)
    transition_order(engine, order_id, "OPEN", reason="venue accepted")

    rows = queries.orders(engine)
    assert len(rows) == 1
    assert rows[0]["state"] == "OPEN"

    body = client.get("/").text
    assert "No orders placed yet" not in body
    assert "open" in body and "SOL" in body


def test_an_empty_orders_table_explains_itself(client: TestClient) -> None:
    """The question this page kept prompting was "why are there no trades?"."""
    body = client.get("/").text
    assert "No orders placed yet" in body
    # And it must say which of the two reasons applies rather than leaving it ambiguous.
    assert ("Not armed" in body) or ("Armed against the" in body)


def test_the_ui_command_still_accepts_the_retired_auto_host() -> None:
    """A deployed systemd unit outlives the code that wrote it.

    `update.sh` syncs code and `install-units.sh` syncs units, so between the two a service can
    pass an argument this binary no longer understands. `--host auto` shipped for one commit and
    took the dashboard down with "Temporary failure in name resolution" when it was removed
    (F-135). Retiring an option means accepting it, not rejecting it.
    """
    from typer.testing import CliRunner

    from argus.cli import app

    called: dict[str, object] = {}
    import argus.ui.app as ui_app

    original = ui_app.serve
    try:
        ui_app.serve = lambda **kwargs: called.update(kwargs)  # type: ignore[assignment]
        result = CliRunner().invoke(app, ["ui", "--host", "auto"])
    finally:
        ui_app.serve = original  # type: ignore[assignment]

    assert result.exit_code == 0, result.output
    assert called["host"] == "127.0.0.1"


def test_a_manager_that_stopped_running_is_shouted_about(
    client: TestClient, engine: Engine
) -> None:
    """F-141: the tile said "6 days ago" and was ignored for 6.7 days.

    The page must do the subtraction itself, and must say what the absence of refusals means --
    an empty decisions table reads as discipline when it is really silence.
    """
    _run(engine)  # NOW is well past manager_max_gap_hours
    body = client.get("/").text
    assert "NOT RUNNING" in body
    assert "argus-manager.timer" in body
    assert "nothing was asked" in body


def test_a_manager_that_ran_recently_raises_no_band(client: TestClient, engine: Engine) -> None:
    """The counterpart that stops this from being decoration: it must be able to stay quiet."""
    run_id = new_id()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r,'manager','demo',:t,'OK',1,'demo')"
            ),
            {"r": run_id, "t": datetime.now(UTC).isoformat()},
        )
    body = client.get("/").text
    assert "NOT RUNNING" not in body


def test_the_dashboard_and_the_alert_share_one_definition_of_stale(engine: Engine) -> None:
    """Two thresholds is how a dashboard reassures you while an alert screams."""
    from argus.observability import alerts

    _run(engine)
    last = alerts.last_run_at(engine, "manager", venue="demo")
    assert last is not None
    gap_h = (datetime.now(UTC) - last).total_seconds() / 3600
    just_over = gap_h - 0.5
    just_under = gap_h + 0.5
    assert queries.staleness(engine, just_over, venue="demo")["stale"] is True
    assert queries.staleness(engine, just_under, venue="demo")["stale"] is False
    assert alerts.stale_manager(last, datetime.now(UTC), just_under) is None


def test_an_order_shows_the_id_the_venue_gave_it(client: TestClient, engine: Engine) -> None:
    """The Venue id column read `exchange_id` but the query never selected it, so every order
    showed "—" — including ones whose id was stored, which is the id you need to look it up."""
    from argus.execution.lifecycle import record_order, record_venue_ids

    run_id = _run(engine)
    order = {
        "cl_ord_id": "cl-v",
        "symbol": "SOL",
        "side": "buy",
        "order_type": "limit",
        "price": "100",
        "size": "3",
    }
    order_id = record_order(engine, run_id, order)
    record_venue_ids(engine, order_id, {"entry_id": "2981234567890"})

    assert queries.orders(engine)[0]["exchange_id"] == "2981234567890"
    assert "2981234567890" in client.get("/").text


def _invoke_ui(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, str, dict[str, object]]:
    from typer.testing import CliRunner

    import argus.ui.app as ui_app
    from argus.cli import app

    called: dict[str, object] = {}
    monkeypatch.setattr(ui_app, "serve", lambda **kwargs: called.update(kwargs))
    result = CliRunner().invoke(app, ["ui", *args])
    return result.exit_code, result.output, called


@pytest.mark.parametrize("host", ["0.0.0.0", "100.64.1.2", "::", "[::]", "example.org"])
def test_the_ui_command_refuses_a_non_loopback_bind(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """P-N10 / P-14: a non-local bind only printed a yellow line and then served anyway.

    Nothing authenticates the journal, and exposure belongs to `tailscale serve` (F-134), so a
    wider bind is refused outright unless someone says, in so many words, that they mean it.
    """
    code, output, called = _invoke_ui(monkeypatch, "--host", host)
    assert code != 0
    assert called == {}
    assert "--allow-remote" in output


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2", "auto"])
def test_the_ui_command_accepts_every_loopback_spelling(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    code, output, called = _invoke_ui(monkeypatch, "--host", host)
    assert code == 0, output
    assert called["host"] != "auto"


def test_a_bracketed_ipv6_loopback_reaches_serve_without_its_brackets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`[::1]` passed the loopback check with its brackets stripped, then reached serve() with
    them still on: an AF_INET6 bind of '[::1]' fails name resolution, so the unit crash-loops on
    a host the check had approved. The URL it prints needs the brackets back."""
    code, output, called = _invoke_ui(monkeypatch, "--host", "[::1]")
    assert code == 0, output
    assert called["host"] == "::1"
    assert "http://[::1]:8787" in output


def test_allow_remote_is_the_explicit_way_past_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    code, output, called = _invoke_ui(monkeypatch, "--host", "0.0.0.0", "--allow-remote")
    assert code == 0, output
    assert called["host"] == "0.0.0.0"
    # Still said out loud: the flag removes the refusal, not the fact.
    assert "nothing authenticates" in output


_DOCS = Path(__file__).resolve().parents[2] / "docs"


@pytest.mark.parametrize("doc", ["ARCHITECTURE.md", "OPEN_QUESTIONS.md", "UI_DESIGN.md"])
def test_the_docs_give_one_answer_for_dashboard_access(doc: str) -> None:
    """P-14: the code binds 127.0.0.1 behind `tailscale serve`, but four docs still said to bind
    the Tailscale interface with basic auth. Whoever builds from them would add the tailnet bind
    that `--allow-remote` now lets through, or an auth layer nothing here implements."""
    body = (_DOCS / doc).read_text().lower()
    assert "basic auth" not in body
    assert "tailscale interface" not in body
    assert "tailscale serve" in body


def _watchdog_row(engine: Engine, status: str, started: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                # Attributed like a real tick (config_hash = venue, F-131): the page shows one
                # account at a time, and an unattributed row belongs to neither view.
                " VALUES (:r,'watchdog','demo',:t,:s,0,'demo')"
            ),
            {"r": new_id(), "t": started.isoformat(), "s": status},
        )


def test_a_skipped_watchdog_tick_does_not_count_as_the_last_watchdog(
    client: TestClient, engine: Engine
) -> None:
    """J-4 journals SKIPPED ticks to expose a gap. Reading MAX(started_at) over every status
    showed the watchdog as having just run while no position had been verified for hours."""
    from datetime import timedelta

    now = datetime.now(UTC)
    verified = now - timedelta(hours=3)
    _watchdog_row(engine, "OK", verified)
    _watchdog_row(engine, "FAILED", now - timedelta(minutes=45))
    _watchdog_row(engine, "SKIPPED", now - timedelta(minutes=5))

    cadence = queries.run_cadence(engine)["watchdog"]
    assert cadence["last"] == verified.isoformat()
    assert cadence["attempted"] == (now - timedelta(minutes=5)).isoformat()
    body = client.get("/").text
    tile = body[body.index("Last watchdog") :][:400]
    assert "3h ago" in tile and "SKIPPED" in tile, tile
