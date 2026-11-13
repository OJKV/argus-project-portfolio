"""Read-only views over the journal for the dashboard.

Every function here is a SELECT. The dashboard has no writes, no forms and no POST routes, which
is a safety property rather than a missing feature: a page that cannot place, cancel or resize
anything is one that can be shown to anyone, left open on a laptop, or port-forwarded during a
demo without becoming a way into the account.

Nothing recomputes. What is shown is what was written at the time the decision was made — the
same rows `argus replay` reads — so the dashboard cannot disagree with the ledger.

One journal holds two accounts. Every venue-scoped view takes `venue` ("demo" | "live"; None means
both) and attributes a row through the run that wrote it: `runs.config_hash` carries the venue for
manager, review and watchdog runs (F-131), and orders, plans, equity and position snapshots all
hang off a run. Runs written before that carry `-`; they belong to neither view, and
`unattributed()` counts them so the page can say so rather than file them under a guess.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, text

from argus.observability import alerts


def _loads(value: Any, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


VENUES = ("demo", "live")

# Every scoped query below filters with this one predicate on the run that wrote the row, so a
# `None` venue (both accounts) and a named one go through the same SQL rather than two variants
# that can drift apart.
_ON_VENUE = "(:venue IS NULL OR r.config_hash = :venue)"


def recent_runs(engine: Engine, limit: int = 25, venue: str | None = None) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT r.run_id, r.kind, r.mode, r.started_at, r.finished_at, r.status,"
                    "       r.shadow, r.config_hash AS venue,"
                    "       (SELECT COUNT(*) FROM dossiers d WHERE d.run_id = r.run_id) dossiers,"
                    "       (SELECT COUNT(*) FROM plans p WHERE p.run_id = r.run_id) plans,"
                    "       (SELECT COUNT(*) FROM llm_calls l WHERE l.run_id = r.run_id) calls"
                    f" FROM runs r WHERE {_ON_VENUE} ORDER BY r.started_at DESC LIMIT :n"
                ),
                {"n": limit, "venue": venue},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def run_detail(engine: Engine, run_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        run = (
            conn.execute(text("SELECT * FROM runs WHERE run_id = :r"), {"r": run_id})
            .mappings()
            .first()
        )
        if run is None:
            return {}
        regime = (
            conn.execute(
                text("SELECT * FROM regime_snapshots WHERE run_id = :r LIMIT 1"), {"r": run_id}
            )
            .mappings()
            .first()
        )
        dossiers = (
            conn.execute(
                text(
                    "SELECT symbol, source, tokens, trade_critical, gaps FROM dossiers"
                    " WHERE run_id = :r ORDER BY symbol"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )
        decisions = (
            conn.execute(
                text(
                    "SELECT p.plan_id, p.symbol, p.side, p.state, p.step, p.payload,"
                    "       g.verdict, g.reasons"
                    " FROM plans p LEFT JOIN gate_results g ON g.plan_id = p.plan_id"
                    " WHERE p.run_id = :r ORDER BY p.created_at"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )
        calls = (
            conn.execute(
                text(
                    "SELECT agent, provider || '/' || model AS model, status, COUNT(*) n,"
                    "       CAST(AVG(latency_ms) AS INTEGER) ms"
                    " FROM llm_calls WHERE run_id = :r GROUP BY agent, model, status"
                    " ORDER BY agent"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )

    return {
        "run": dict(run),
        "regime": dict(regime) if regime else None,
        "dossiers": [dict(d) | {"gaps": _loads(d["gaps"], [])} for d in dossiers],
        "decisions": [
            dict(d)
            | {
                "plan": _loads(d["payload"], {}),
                "reasons": (_loads(d["reasons"], {}) or {}).get("reasons", []),
            }
            for d in decisions
        ],
        "calls": [dict(c) for c in calls],
    }


def orders(engine: Engine, limit: int = 40, venue: str | None = None) -> list[dict[str, Any]]:
    """Every order the system has placed, with its DERIVED state.

    `orders` holds one write-once row per order; the state lives in `position_events`, appended on
    every transition. Reading `orders.state` would say NEW forever, which is exactly the kind of
    quiet disagreement between a dashboard and a ledger that makes a dashboard untrustworthy.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT o.order_id, o.created_at, o.symbol, o.side, o.kind, o.order_type,"
                    "       o.price, o.size, o.cl_ord_id, o.exchange_id, o.run_id,"
                    "       (SELECT e.to_state FROM position_events e"
                    "         WHERE e.machine = 'order'"
                    "           AND json_extract(e.detail, '$.order_id') = o.order_id"
                    "         ORDER BY e.created_at DESC LIMIT 1) AS derived_state"
                    " FROM orders o JOIN runs r ON r.run_id = o.run_id"
                    f" WHERE {_ON_VENUE} ORDER BY o.created_at DESC LIMIT :n"
                ),
                {"n": limit, "venue": venue},
            )
            .mappings()
            .all()
        )
    return [dict(row) | {"state": row["derived_state"] or "NEW"} for row in rows]


def fills(engine: Engine, limit: int = 40) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT created_at, symbol, side, price, size, fee, order_id"
                    " FROM fills ORDER BY created_at DESC LIMIT :n"
                ),
                {"n": limit},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def open_positions(engine: Engine, venue: str | None = None) -> list[dict[str, Any]]:
    """Open positions as the venue last showed them, with their age.

    The age matters as much as the numbers: these come from the last watchdog tick, not from a
    live fetch, so a stale row and a fresh one look identical without it.

    "Last" is per account. The newest snapshot of a symbol on the OTHER account says nothing
    about this one — a live watchdog that no longer sees a demo position writes CLOSED for it,
    and that is a fact about the live account, not evidence the demo position went away.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT p.* FROM positions_snapshots p"
                    " JOIN runs r ON r.run_id = p.run_id"
                    " JOIN (SELECT s.symbol, MAX(s.created_at) AS t FROM positions_snapshots s"
                    "        JOIN runs r ON r.run_id = s.run_id"
                    f"        WHERE {_ON_VENUE} GROUP BY s.symbol) last"
                    "   ON last.symbol = p.symbol AND last.t = p.created_at"
                    f" WHERE p.state = 'OPEN' AND {_ON_VENUE} ORDER BY p.symbol"
                ),
                {"venue": venue},
            )
            .mappings()
            .all()
        )
    return [dict(row) | {"unprotected": row["protection"] == "NONE"} for row in rows]


def unprotected_positions(engine: Engine) -> list[dict[str, Any]]:
    """Every open position the last watchdog tick saw without a stop, on EITHER account.

    Not venue-scoped, like the kill switch and staleness: a missing exchange-side stop
    (non-negotiable #4) is a fact about the system, and a view preference remembered for a year
    must not be able to hide one. Each account is read on its own, so a newer snapshot of the same
    symbol on the other account cannot mask it.
    """
    return [
        row | {"venue": venue}
        for venue in VENUES
        for row in open_positions(engine, venue=venue)
        if row["unprotected"]
    ]


# A closed trade belongs to the account its entry was placed on: the run that wrote its plan. The
# review run that judged it is the fallback for a plan written before runs recorded a venue —
# also a journalled fact, not an inference, since that run saw the trade on its own account.
_POSTMORTEM_VENUE = (
    "CASE WHEN opened.config_hash IN ('demo', 'live') THEN opened.config_hash"
    "     WHEN judged.config_hash IN ('demo', 'live') THEN judged.config_hash END"
)
_POSTMORTEM_RUNS = (
    " FROM postmortems pm"
    " LEFT JOIN plans pl ON pl.plan_id = pm.plan_id"
    " LEFT JOIN runs opened ON opened.run_id = pl.run_id"
    " LEFT JOIN runs judged ON judged.run_id = pm.run_id"
)


def postmortems(engine: Engine, limit: int = 20, venue: str | None = None) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT * FROM ("
                    "  SELECT pm.created_at, pm.symbol, pm.outcome_r, pm.exit_reason,"
                    "         pm.process_verdict, pm.would_take_again, pm.entry_timing,"
                    f"         pm.stop_placement, {_POSTMORTEM_VENUE} AS venue"
                    f"  {_POSTMORTEM_RUNS}"
                    ") WHERE (:venue IS NULL OR venue = :venue)"
                    " ORDER BY created_at DESC LIMIT :n"
                ),
                {"n": limit, "venue": venue},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def agent_scores(engine: Engine, limit: int = 40) -> list[dict[str, Any]]:
    """Latest golden-set result per model — the evidence behind every leader in models.yaml."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT provider || '/' || model AS model, tier, cases, correct, invented,"
                    "       schema_fail, errors, p50_ms, usable, created_at"
                    " FROM model_evals WHERE (provider, model, created_at) IN"
                    "   (SELECT provider, model, MAX(created_at) FROM model_evals"
                    "     GROUP BY provider, model)"
                    " ORDER BY usable DESC, CAST(correct AS REAL) / NULLIF(cases, 0) DESC"
                    " LIMIT :n"
                ),
                {"n": limit},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def active_lessons(engine: Engine) -> list[dict[str, Any]]:
    from argus.journal import lessons as memory

    return [
        {
            "scope": lesson.scope,
            "lesson": lesson.lesson,
            "support": lesson.support,
            "contradiction": lesson.contradiction,
        }
        for lesson in memory.active(engine)
    ]


def venue_state(engine: Engine) -> dict[str, Any]:
    """What the JOURNAL says about the last real run, next to what this process believes.

    Two sources on purpose. `get_settings()` is cached for the life of the process and the
    dashboard is restarted only by a deploy, so a page left open can be hours behind the account
    the system is actually trading. The row was written by the process that traded; the config is
    what this page happens to hold. When they disagree, the run is the truth and the page says so
    loudly — quietly trusting its own config is how a page tells someone "demo" while real money
    moves.
    """
    from argus.config import get_settings

    settings = get_settings()
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT mode, shadow, config_hash AS venue, started_at, status"
                    " FROM runs WHERE kind = 'manager' ORDER BY started_at DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )

    last = dict(row) if row else None
    state: dict[str, Any] = {
        "last_run": last,
        "config_mode": settings.mode,
        "config_venue": settings.venue,
        "config_armed": settings.is_live,
        "halt_path": str(settings.halt_file),
        "halt_present": settings.halt_file.exists(),
    }
    if last is None:
        state["agreement"] = "unknown"
        state["armed"] = False
        state["venue"] = None
        return state

    state["armed"] = last["mode"] in ("approve", "auto") and not last["shadow"]
    state["venue"] = last["venue"] if last["venue"] in ("demo", "live") else None
    if state["venue"] is None:
        # Runs written before the venue was journalled (F-131) carry `-`. That is "I do not know",
        # which is not the same as "these conflict" — calling it a disagreement would cry wolf on
        # every historical row and teach the reader to ignore the one band that matters.
        state["agreement"] = "unattributed"
    elif state["venue"] == settings.venue and last["mode"] == settings.mode:
        state["agreement"] = "agree"
    else:
        state["agreement"] = "disagree"
    return state


def decisions(engine: Engine, limit: int = 40, venue: str | None = None) -> list[dict[str, Any]]:
    """Every plan and what the gate did with it — the refusal ledger.

    The most valuable table in the system today: it holds the reason each trade did NOT happen,
    verbatim from the gate that refused it.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT p.plan_id, p.created_at, p.symbol, p.side, p.payload,"
                    "       g.verdict, g.reasons, r.config_hash AS venue, r.mode, r.shadow,"
                    "       p.run_id, rs.label AS regime"
                    " FROM plans p"
                    " LEFT JOIN gate_results g ON g.plan_id = p.plan_id"
                    " JOIN runs r ON r.run_id = p.run_id"
                    " LEFT JOIN regime_snapshots rs ON rs.run_id = p.run_id"
                    f" WHERE {_ON_VENUE} ORDER BY p.created_at DESC LIMIT :n"
                ),
                {"n": limit, "venue": venue},
            )
            .mappings()
            .all()
        )

    out: list[dict[str, Any]] = []
    for row in rows:
        plan = _loads(row["payload"], {}) or {}
        gate = _loads(row["reasons"], {}) or {}
        reasons = gate.get("reasons") or []
        refusal = next((r for r in reasons if r.get("verdict") == "REJECT"), None)
        sized = gate.get("sized")
        entry = plan.get("entry") or [None, None]
        out.append(
            dict(row)
            | {
                "thesis": plan.get("thesis") or "",
                "entry_low": entry[0],
                "entry_high": entry[1] if len(entry) > 1 else None,
                "stop": plan.get("stop"),
                "targets": plan.get("targets") or [],
                "refused_gate": refusal.get("gate") if refusal else None,
                "refused_name": refusal.get("name") if refusal else None,
                "refused_detail": refusal.get("detail") if refusal else "",
                # Absent on anything refused before sizing ran — rendered as "not reached"
                # rather than as zeros, which would read as a decision nobody made.
                "sized": sized,
            }
        )
    return out


def refusal_histogram(
    engine: Engine, days: int = 14, venue: str | None = None
) -> list[dict[str, Any]]:
    """Which gate refuses most often. A pure count over what already happened."""
    rows = decisions(engine, limit=500, venue=venue)
    tally: dict[str, int] = {}
    for row in rows:
        if row["refused_gate"] is None:
            continue
        key = f"gate {row['refused_gate']} · {row['refused_name']}"
        tally[key] = tally.get(key, 0) + 1
    total = sum(tally.values()) or 1
    return [
        {"gate": k, "count": v, "pct": round(100 * v / total)}
        for k, v in sorted(tally.items(), key=lambda kv: -kv[1])
    ]


def equity(engine: Engine, limit: int = 60, venue: str | None = None) -> dict[str, Any]:
    """Latest equity, its age, and the 30-day drawdown of one account.

    Peak and latest come from the same account. A demo peak of $1 000 over a live balance of $250
    is not a 75% live drawdown; it is two accounts subtracted from each other. This is NOT the
    number the kill switch acts on — `kill_switch()` is, and the page shows both.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT e.created_at, e.equity, r.config_hash AS venue"
                    " FROM equity_snapshots e LEFT JOIN runs r ON r.run_id = e.run_id"
                    f" WHERE {_ON_VENUE} ORDER BY e.created_at DESC LIMIT :n"
                ),
                {"n": limit, "venue": venue},
            )
            .mappings()
            .all()
        )
        peak = conn.execute(
            text(
                "SELECT MAX(CAST(e.equity AS REAL)) FROM equity_snapshots e"
                " LEFT JOIN runs r ON r.run_id = e.run_id"
                f" WHERE e.created_at >= datetime('now', '-30 days') AND {_ON_VENUE}"
            ),
            {"venue": venue},
        ).scalar()

    if not rows:
        return {"points": [], "latest": None, "drawdown_pct": None, "venue": None}
    latest = float(rows[0]["equity"])
    high = max(float(peak or latest), latest)
    return {
        "points": [dict(r) for r in reversed(rows)],
        "latest": latest,
        "as_of": rows[0]["created_at"],
        "venue": rows[0]["venue"] if rows[0]["venue"] in ("demo", "live") else None,
        # One reading cannot describe a drawdown, and rendering 0.0% would claim it can.
        "drawdown_pct": None if len(rows) < 2 else round((high - latest) / high * 100, 2),
    }


def kill_switch(engine: Engine) -> dict[str, Any] | None:
    """The drawdown the watchdog's kill switch acts on, as of the last equity it recorded.

    Computed with the watchdog's own `equity_peak_30d` and threshold, not a copy of them, so the
    page cannot drift from what decides a flatten. Not scoped by the venue switch, like the HALT
    file: whichever account is viewed, this is the number that can flatten its account. It is
    the reading's own account's figure, because that is all the watchdog reads (F-149).
    """
    from decimal import Decimal

    from argus.runs.watchdog import DD_HALT_PCT, equity_peak_30d

    with engine.connect() as conn:
        last = (
            conn.execute(
                text(
                    "SELECT e.created_at, e.equity, r.config_hash AS venue"
                    " FROM equity_snapshots e LEFT JOIN runs r ON r.run_id = e.run_id"
                    " ORDER BY e.created_at DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
        if last is None:
            return None
    venue = last["venue"]
    if venue not in VENUES:
        return None  # a pre-F-131 reading belongs to no account, and the watchdog reads neither
    current = Decimal(str(last["equity"]))
    peak = equity_peak_30d(engine, current, venue=venue)
    if peak <= 0:
        return None
    drawdown = (peak - current) / peak * Decimal("100")
    return {
        "drawdown_pct": round(float(drawdown), 2),
        "halt_pct": str(DD_HALT_PCT),
        "tripped": drawdown >= DD_HALT_PCT,
        "venue": venue,
        "as_of": last["created_at"],
    }


def run_cadence(engine: Engine, venue: str | None = None) -> dict[str, Any]:
    """Per kind, on one account: the last run that finished OK, and the last attempt of any status.

    Per account because a watchdog watching demo has not looked at live, and a tile that pooled
    them would say live was being watched. `last` is OK only: J-4 journals SKIPPED watchdog ticks
    to expose a gap, and counting them (or a FAILED or ABANDONED run) as the last watchdog showed
    one minutes ago while no position had been verified for hours.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT r.kind, MAX(CASE WHEN r.status = 'OK' THEN r.started_at END) AS last,"
                    " MAX(r.started_at) AS attempted, COUNT(*) AS n,"
                    " (SELECT x.status FROM runs x WHERE x.kind = r.kind"
                    "  AND (:venue IS NULL OR x.config_hash = :venue)"
                    "  ORDER BY x.started_at DESC, x.rowid DESC LIMIT 1) AS attempt_status"
                    f" FROM runs r WHERE {_ON_VENUE} GROUP BY r.kind"
                ),
                {"venue": venue},
            )
            .mappings()
            .all()
        )
    return {
        row["kind"]: {
            "last": row["last"],
            "attempted": row["attempted"],
            "attempt_status": row["attempt_status"],
            "count": row["n"],
        }
        for row in rows
    }


def unattributed(engine: Engine) -> dict[str, int]:
    """Rows that belong to neither view, by table.

    Written by runs that carry no venue (`-`, from before F-131) or no run at all. Filing them
    under demo or live would be a guess; leaving them out silently would make history vanish.
    Counted, so the page can name them.
    """
    unknown = "COALESCE(r.config_hash, '-') NOT IN ('demo', 'live')"
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    f"SELECT (SELECT COUNT(*) FROM runs r WHERE {unknown}) AS runs,"
                    "  (SELECT COUNT(*) FROM plans p LEFT JOIN runs r ON r.run_id = p.run_id"
                    f"    WHERE {unknown}) AS decisions,"
                    "  (SELECT COUNT(*) FROM orders o LEFT JOIN runs r ON r.run_id = o.run_id"
                    f"    WHERE {unknown}) AS orders,"
                    "  (SELECT COUNT(*) FROM equity_snapshots e"
                    f"    LEFT JOIN runs r ON r.run_id = e.run_id WHERE {unknown}) AS equity,"
                    "  (SELECT COUNT(*) FROM positions_snapshots s"
                    f"    LEFT JOIN runs r ON r.run_id = s.run_id WHERE {unknown}) AS positions,"
                    f"  (SELECT COUNT(*) {_POSTMORTEM_RUNS}"
                    f"    WHERE ({_POSTMORTEM_VENUE}) IS NULL) AS postmortems"
                )
            )
            .mappings()
            .one()
        )
    return {key: int(value or 0) for key, value in row.items()}


def agent_map() -> list[dict[str, Any]]:
    """Which model leads which role, and what the role's failure means."""
    from argus.llm.router import agent_policy, tier_refs

    out: list[dict[str, Any]] = []
    for agent in (
        "regime", "catalyst", "analyst", "redteam", "riskdebate",
        "trader", "consensus", "reviewer", "postmortem", "reflector",
    ):  # fmt: skip
        policy = agent_policy(agent)
        refs = tier_refs(policy.tier)
        leader = next(
            (r for r in refs if r.family == policy.families[0]), refs[0] if refs else None
        )
        out.append(
            {
                "agent": agent,
                "tier": policy.tier,
                "criticality": policy.criticality.value,
                "families": ", ".join(policy.families),
                "leader": leader.label if leader else "-",
                "judges": policy.counterpart or "",
            }
        )
    return out


def staleness(
    engine: Engine, max_gap_h: float, now: datetime | None = None, *, venue: str
) -> dict[str, Any]:
    """Is anything actually running the manager? Answered by code, not by reading a tile.

    F-141: the `argus-manager.timer` was installed but disabled for 6.7 days. The dashboard
    already showed "last manager run — 6 days ago", and the number was correct; nobody subtracted
    it from the clock. The watchdog's `manager_stale` alert *did* fire, 364 times, into a Telegram
    channel that was dead.

    So the comparison moves to where it cannot be skipped, and reuses the alert's own threshold
    rather than inventing a second definition of stale — two definitions is how a dashboard ends
    up reassuring you while an alert screams. For the same reason it reads the same account the
    alert does, `venue` (F-150): a demo run is not the live manager running.
    """
    last = alerts.last_run_at(engine, "manager", venue=venue)
    alert = alerts.stale_manager(last, now or datetime.now(UTC), max_gap_h)
    if alert is None:
        return {"stale": False}
    return {
        "stale": True,
        "detail": alert.message,
        "last": last.isoformat() if last else None,
        "max_gap_h": max_gap_h,
    }
