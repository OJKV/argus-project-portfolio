"""The Demo | Live switcher: one journal, two accounts, and a page that never mixes them.

The journal holds rows from both the demo and the live account. Before the switcher every panel
summed them, so a demo drawdown, a demo order or a demo refusal could sit on the page as if it were
the live account's — and the reverse once live trading starts. The switch is a link and a cookie,
not a form: the page stays read-only (GET only), and choosing what to LOOK at must never be
mistakable for choosing what the system TRADES.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from argus.journal.db import new_id, open_journal
from argus.ui import queries

NOW = datetime.now(UTC) - timedelta(minutes=5)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return open_journal(tmp_path / "journal.db")


@pytest.fixture
def client(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from argus.ui import app as app_module

    monkeypatch.setattr(app_module, "open_journal_readonly", lambda *a, **k: engine)
    return TestClient(app_module.create_app())


def _words(html: str) -> str:
    """The page as a reader sees it: tags dropped, whitespace collapsed."""
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _seed(
    engine: Engine,
    venue: str,
    tag: str,
    *,
    equity: str,
    kind: str = "manager",
    mode: str = "demo",
    shadow: int = 1,
    gate: int = 3,
    gate_name: str = "structure",
    when: datetime = NOW,
) -> str:
    """One run on `venue` with a refused plan, an order, a position, a closed trade and an equity
    reading — every symbol carries `tag` so a test can see which account a row came from."""
    from argus.execution.lifecycle import record_order
    from argus.journal import positions

    run_id = new_id()
    plan_id = new_id()
    stamp = when.isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (run_id, kind, mode, started_at, status, shadow, config_hash)"
                " VALUES (:r, :k, :m, :t, 'OK', :s, :v)"
            ),
            {"r": run_id, "k": kind, "m": mode, "t": stamp, "s": shadow, "v": venue},
        )
        conn.execute(
            text(
                "INSERT INTO plans (plan_id, run_id, created_at, mode, symbol, side, state,"
                " payload, prev_hash, row_hash) VALUES (:p, :r, :t, :m, :sym, 'long',"
                " 'PROPOSED', '{}', '', '')"
            ),
            {"p": plan_id, "r": run_id, "t": stamp, "m": mode, "sym": f"{tag}PLAN"},
        )
        conn.execute(
            text(
                "INSERT INTO gate_results (gate_result_id, plan_id, run_id, created_at, verdict,"
                " reasons, prev_hash, row_hash) VALUES (:g, :p, :r, :t, 'REJECTED', :j, '', '')"
            ),
            {
                "g": new_id(),
                "p": plan_id,
                "r": run_id,
                "t": stamp,
                "j": json.dumps(
                    {
                        "reasons": [
                            {"gate": gate, "name": gate_name, "verdict": "REJECT", "detail": "x"}
                        ]
                    }
                ),
            },
        )
        conn.execute(
            text(
                "INSERT INTO equity_snapshots (snapshot_id, run_id, created_at, equity)"
                " VALUES (:i, :r, :t, :e)"
            ),
            {"i": new_id(), "r": run_id, "t": stamp, "e": equity},
        )
        conn.execute(
            text(
                "INSERT INTO postmortems (plan_id, created_at, run_id, symbol, outcome_r,"
                " exit_reason, process_verdict, would_take_again, entry_timing, sizing,"
                " stop_placement, report) VALUES (:p, :t, :r, :sym, 1.0, 'tp', 'sound', 1,"
                " 'ok', 'ok', 'ok', '{}')"
            ),
            {"p": plan_id, "t": stamp, "r": run_id, "sym": f"{tag}PM"},
        )
    record_order(
        engine,
        run_id,
        {
            "cl_ord_id": f"cl-{tag}",
            "symbol": f"{tag}ORD",
            "side": "buy",
            "order_type": "limit",
            "price": "100",
            "size": "1",
        },
        plan_id=plan_id,
    )
    positions.record(
        engine,
        run_id,
        [{"symbol": f"{tag}POS", "side": "long", "size": "1", "entry_price": 1, "mark_price": 1}],
        now=when,
    )
    return run_id


@pytest.fixture
def both(engine: Engine) -> dict[str, str]:
    return {
        "demo": _seed(engine, "demo", "DMO", equity="1000"),
        "live": _seed(engine, "live", "LVE", equity="250", gate=7, gate_name="funding"),
    }


# --- every panel, one venue at a time -----------------------------------------------------------


@pytest.mark.parametrize(
    ("venue", "mine", "theirs"), [("demo", "DMO", "LVE"), ("live", "LVE", "DMO")]
)
def test_every_query_returns_only_the_selected_venue(
    engine: Engine, both: dict[str, str], venue: str, mine: str, theirs: str
) -> None:
    symbols = {
        "orders": [r["symbol"] for r in queries.orders(engine, venue=venue)],
        "positions": [r["symbol"] for r in queries.open_positions(engine, venue=venue)],
        "postmortems": [r["symbol"] for r in queries.postmortems(engine, venue=venue)],
        "decisions": [r["symbol"] for r in queries.decisions(engine, venue=venue)],
    }
    assert symbols == {
        "orders": [f"{mine}ORD"],
        "positions": [f"{mine}POS"],
        "postmortems": [f"{mine}PM"],
        "decisions": [f"{mine}PLAN"],
    }, theirs

    assert [r["run_id"] for r in queries.recent_runs(engine, venue=venue)] == [both[venue]]
    assert queries.run_cadence(engine, venue=venue)["manager"]["count"] == 1
    equity = queries.equity(engine, venue=venue)
    assert equity["latest"] == (1000.0 if venue == "demo" else 250.0)
    assert equity["venue"] == venue
    histogram = [h["gate"] for h in queries.refusal_histogram(engine, venue=venue)]
    assert histogram == (["gate 3 · structure"] if venue == "demo" else ["gate 7 · funding"])


def test_the_drawdown_peak_is_the_selected_accounts_own(engine: Engine) -> None:
    """A $1 000 demo peak next to a $250 live balance is not a 75% live drawdown."""
    _seed(engine, "demo", "DMO", equity="1000")
    _seed(engine, "live", "LVE", equity="250")
    _seed(engine, "live", "LV2", equity="240", when=NOW + timedelta(minutes=1))
    live = queries.equity(engine, venue="live")
    assert live["latest"] == 240.0
    assert live["drawdown_pct"] == 4.0


def test_the_kill_switch_acts_on_the_accounts_own_drawdown(
    client: TestClient, engine: Engine
) -> None:
    """The kill switch reads only its own account's peak (review of PR #1, F-149).

    Demo at $10 000, then live at $200 and $198. Read across both accounts, the watchdog computed
    98%, wrote HALT and flattened the live book, and this page showed that 98%. Scoped, the
    watchdog acts on 1% — and the page shows the figure from the watchdog's own function, so the
    two cannot disagree silently, whichever account is viewed.
    """
    from decimal import Decimal

    from argus.runs.watchdog import equity_peak_30d

    _seed(engine, "demo", "DMO", equity="10000", kind="watchdog")
    _seed(engine, "live", "LVE", equity="200", kind="watchdog", when=NOW + timedelta(minutes=1))
    _seed(engine, "live", "LV2", equity="198", kind="watchdog", when=NOW + timedelta(minutes=2))
    assert equity_peak_30d(engine, Decimal("198"), venue="live") == Decimal("200")
    assert queries.equity(engine, venue="live")["drawdown_pct"] == 1.0

    for viewing in ("live", "demo"):
        page = _words(client.get(f"/?venue={viewing}").text)
        assert "acts on LIVE: 1.0% drawdown · halts at 15%" in page, viewing
        assert "other account" not in page, viewing

    kill = queries.kill_switch(engine)
    assert kill is not None
    assert (kill["drawdown_pct"], kill["venue"], kill["tripped"]) == (1.0, "live", False)


def test_a_reading_from_no_account_is_not_shown_as_the_kill_switch(
    client: TestClient, engine: Engine
) -> None:
    """The watchdog reads only `demo` or `live` equity (F-149). A pre-F-131 reading tagged `-`
    is acted on by nothing, so the page must not say the kill switch "acts on" it."""
    _seed(engine, "-", "OLD", equity="500", kind="watchdog")
    assert queries.kill_switch(engine) is None
    assert "acts on" not in _words(client.get("/?venue=demo").text)


def test_one_account_alone_gets_no_other_account_note(client: TestClient, engine: Engine) -> None:
    _seed(engine, "live", "LVE", equity="200", kind="watchdog")
    _seed(engine, "live", "LV2", equity="198", kind="watchdog", when=NOW + timedelta(minutes=1))
    page = _words(client.get("/?venue=live").text)
    assert "acts on LIVE: 1.0% drawdown · halts at 15%" in page
    assert "other account" not in page


def test_a_newer_snapshot_on_the_other_account_does_not_hide_an_open_position(
    client: TestClient, engine: Engine
) -> None:
    """The latest snapshot of a symbol is the latest within its own account (review of PR #1).

    Every other seed gives each account its own symbols, so nothing pinned it. The trigger: the
    same instrument is open on demo, and a newer CLOSED row for it exists under a live run — as a
    live watchdog wrote before `newly_closed` was scoped to its account (F-149), and as a journal
    from then still holds. The demo position, possibly an unprotected one, must not vanish from
    the demo view.
    """
    from argus.journal import positions

    demo_run = _seed(engine, "demo", "DMO", equity="1000", kind="watchdog")
    live_run = _seed(
        engine, "live", "LVE", equity="250", kind="watchdog", when=NOW + timedelta(minutes=1)
    )
    shared = {"symbol": "SHARED", "side": "long", "size": "1", "entry_price": 1, "mark_price": 1}
    positions.record(engine, demo_run, [shared], now=NOW)
    # A live run's CLOSED row for the demo position, written directly: the watchdog no longer can.
    assert "SHARED" not in [c.symbol for c in positions.newly_closed(engine, [], venue="live")]
    demo_row = positions.latest_open(engine, venue="demo")["SHARED"]
    positions.mark_closed(
        engine,
        live_run,
        [positions.Closed(symbol="SHARED", last_seen=demo_row)],
        now=NOW + timedelta(minutes=2),
    )

    assert "SHARED" in [p["symbol"] for p in queries.open_positions(engine, venue="demo")]
    assert "SHARED" not in [p["symbol"] for p in queries.open_positions(engine, venue="live")]
    assert "SHARED" in client.get("/?venue=demo").text
    assert "SHARED" not in client.get("/?venue=live").text


def test_the_page_shows_only_the_selected_venue(client: TestClient, both: dict[str, str]) -> None:
    live = client.get("/?venue=live").text
    assert "LVEORD" in live and "LVEPOS" in live and "LVEPM" in live and "LVEPLAN" in live
    assert "DMO" not in live
    assert "$250.00" in live and "$1,000.00" not in live
    assert both["demo"] not in live

    demo = client.get("/?venue=demo").text
    assert "DMOORD" in demo and "DMOPOS" in demo and "DMOPM" in demo and "DMOPLAN" in demo
    assert "LVE" not in demo
    assert both["live"] not in demo


def test_a_missing_stop_is_announced_whichever_account_is_viewed(
    client: TestClient, engine: Engine
) -> None:
    """Review of PR #1: an unprotected position (non-negotiable #4) is a fact about the system,
    like the kill switch, not about the account being viewed. owner clicks DEMO once — remembered
    for a year — the system later trades live, a live stop repair fails: the page must still say
    so, on every page and from either view."""
    from argus.journal import positions

    _seed(engine, "demo", "DMO", equity="1000", kind="watchdog")
    live_run = _seed(engine, "live", "LVE", equity="250", kind="watchdog")
    naked = {"symbol": "NAKED", "side": "long", "size": "1", "entry_price": 1, "mark_price": 1}
    positions.record(engine, live_run, [naked], unprotected={"NAKED"}, now=NOW)

    for path in ("/?venue=demo", "/agents?venue=demo", f"/run/{live_run}?venue=demo"):
        assert "NO STOP: NAKED on LIVE" in _words(client.get(path).text), path
    assert [(p["symbol"], p["venue"]) for p in queries.unprotected_positions(engine)] == [
        ("NAKED", "live")
    ]


def test_no_missing_stop_alarm_when_every_stop_is_attached(
    client: TestClient, engine: Engine, both: dict[str, str]
) -> None:
    assert queries.unprotected_positions(engine) == []
    assert "NO STOP" not in client.get("/?venue=demo").text


def test_cadence_is_per_account(client: TestClient, engine: Engine) -> None:
    """A watchdog watching the demo account has not looked at the live one."""
    _seed(engine, "demo", "DMO", equity="1000", kind="watchdog")
    live = _words(client.get("/?venue=live").text)
    assert "Last watchdog never" in live
    demo = _words(client.get("/?venue=demo").text)
    assert "Last watchdog never" not in demo


# --- choosing, remembering, defaulting ----------------------------------------------------------


def test_the_choice_is_remembered_in_a_cookie(client: TestClient, both: dict[str, str]) -> None:
    first = client.get("/?venue=live")
    assert first.cookies.get("argus_venue") == "live"
    # The next visit carries no query string and still shows the live account.
    again = client.get("/")
    assert "LVEORD" in again.text and "DMOORD" not in again.text
    assert "Viewing LIVE" in _words(again.text)
    # And it follows to the other page.
    assert "Viewing LIVE" in _words(client.get("/agents").text)


def test_an_unknown_venue_is_ignored_not_echoed(client: TestClient, both: dict[str, str]) -> None:
    response = client.get("/?venue=<script>")
    assert response.status_code == 200
    assert "argus_venue" not in response.cookies
    assert "<script>" not in response.text
    assert "Viewing DEMO" in _words(response.text)


def test_the_default_follows_the_config_demo(client: TestClient, both: dict[str, str]) -> None:
    body = client.get("/").text
    assert "Viewing DEMO" in _words(body)
    assert "DMOORD" in body and "LVEORD" not in body


def test_the_default_follows_the_config_live(
    client: TestClient, both: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from argus import config

    # The property rather than the env triple, so this pins "the default is whatever the config
    # says the venue is" without depending on which interlocks arming live needs today.
    monkeypatch.setattr(config.Settings, "venue", property(lambda self: "live"))
    assert config.get_settings().venue == "live"

    body = client.get("/").text
    assert "Viewing LIVE" in _words(body)
    assert "LVEORD" in body and "DMOORD" not in body


# --- viewing is not trading ---------------------------------------------------------------------


def test_viewing_live_is_never_confused_with_armed_on_live(
    client: TestClient, engine: Engine
) -> None:
    """The one confusion that matters: looking at the live account while the system trades demo."""
    _seed(engine, "demo", "DMO", equity="1000", mode="approve", shadow=0)
    words = _words(client.get("/?venue=live").text)
    assert "Viewing LIVE" in words
    assert "armed on DEMO" in words
    assert "not the account the system is trading" in words

    same = _words(client.get("/?venue=demo").text)
    assert "Viewing DEMO" in same and "armed on DEMO" in same
    assert "not the account the system is trading" not in same


def test_viewing_while_nothing_is_armed_says_so(client: TestClient, engine: Engine) -> None:
    _seed(engine, "demo", "DMO", equity="1000")  # shadow
    words = _words(client.get("/?venue=live").text)
    assert "Viewing LIVE" in words
    assert "not armed" in words
    assert "armed on" not in words


def test_the_switch_is_two_links_and_no_form(client: TestClient) -> None:
    """Still read-only: no POST route, and the switcher is plain links, not a form."""
    body = client.get("/").text
    assert "<form" not in body
    assert 'href="/?venue=demo"' in body and 'href="/?venue=live"' in body
    mutating = [
        route.path
        for route in client.app.routes  # type: ignore[attr-defined]
        if hasattr(route, "methods") and route.methods - {"GET", "HEAD"}
    ]
    assert mutating == []


def test_a_live_account_with_no_activity_is_an_empty_state_not_an_error(
    client: TestClient, engine: Engine
) -> None:
    _seed(engine, "demo", "DMO", equity="1000")
    response = client.get("/?venue=live")
    assert response.status_code == 200
    words = _words(response.text)
    assert "The live account has not traded yet" in words
    assert "DMO" not in words
    # And with a completely empty journal too.


def test_an_empty_journal_renders_both_views(client: TestClient) -> None:
    for venue in ("demo", "live"):
        response = client.get(f"/?venue={venue}")
        assert response.status_code == 200
        assert f"The {venue} account has not traded yet" in _words(response.text)


def test_rows_with_no_recorded_venue_are_labelled_not_guessed(
    client: TestClient, engine: Engine
) -> None:
    """Runs from before the venue was journalled carry `-` (F-131). Putting them in either view
    would be a guess; dropping them silently would hide history. They are counted and named."""
    _seed(engine, "-", "OLD", equity="500")
    for venue in ("demo", "live"):
        words = _words(client.get(f"/?venue={venue}").text)
        assert "OLD" not in words
        assert "cannot be attributed" in words
    counts = queries.unattributed(engine)
    assert counts["runs"] == 1 and counts["decisions"] == 1 and counts["orders"] == 1
    assert counts["equity"] == 1 and counts["positions"] == 1 and counts["postmortems"] == 1


def test_no_unattributed_note_when_everything_is_attributed(
    client: TestClient, both: dict[str, str]
) -> None:
    assert "cannot be attributed" not in _words(client.get("/").text)


# --- a run page names its account ---------------------------------------------------------------


def test_an_armed_demo_run_is_not_called_live_execution(client: TestClient, engine: Engine) -> None:
    """Armed on the demo account (F-123) is not live execution; only an armed live run is."""
    demo = _words(
        client.get(
            f"/run/{_seed(engine, 'demo', 'DMO', equity='1', mode='approve', shadow=0)}"
        ).text
    )
    assert "live execution" not in demo
    assert "armed on DEMO" in demo

    live = _words(
        client.get(
            f"/run/{_seed(engine, 'live', 'LVE', equity='1', mode='approve', shadow=0)}"
        ).text
    )
    assert "live execution" in live
    assert "LIVE" in live
