"""ForexFactory weekly calendar export (DATA_SOURCES.md §4). Tier 1, no key.

Drives the event-blackout gate (DECISION_LOGIC.md §6 gate 8). The export throttles hard, so
it is fetched at most once per cache window. A blank forecast is *missing*, never zero (trap 3).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json
from argus.data.scrub import scrub

SOURCE = "forexfactory"
TIER: Tier = 1
URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MARKET_MOVING = ("USD",)
# Every impact label the export uses. A label outside this set means the format drifted, and a
# filter that silently matches nothing would read as "no event this week" (D-4).
KNOWN_IMPACTS = frozenset({"high", "medium", "low", "holiday", "non-economic"})

log = structlog.get_logger(__name__)


def fetch_calendar(now: datetime | None = None) -> Observation:
    """This week's export, downgraded to `suspect` when it cannot answer "when is the next
    high-impact event" — callers then treat the calendar as unknown, never as a quiet week.

    The check runs inside the fetch, so a bad export is never cached for the 6 h window and
    a cached one that has gone stale (the Sunday rollover) is fetched again. It runs once more
    here for any path that hands back a body without it.
    """
    moment = now or datetime.now(UTC)
    obs = fetch_json(
        f"{SOURCE}.calendar", URL, TIER, "macro", validate=lambda rows: rows_problem(rows, moment)
    )
    if obs.quality != "ok":
        return obs
    problem = calendar_problem(obs, moment)
    if problem is None:
        return obs
    log.warning("calendar.unusable", reason=problem)
    return obs.model_copy(update={"quality": "suspect", "notes": [*obs.notes, problem]})


def _impact(row: dict[str, Any]) -> str:
    return str(row.get("impact") or "").strip().lower()


def _parse_local(raw: Any) -> datetime | None:
    """The row's time in the export's own offset (US Eastern).

    A time without an offset is format drift, not data: `astimezone` would read it in the
    *host's* zone, putting the same event at different hours on the Mac and the VPS (D-18j).
    """
    try:
        local = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return local if local.tzinfo is not None else None


def _parse_at(raw: Any) -> datetime | None:
    local = _parse_local(raw)
    return local.astimezone(UTC) if local is not None else None


def _week_start(day: date) -> date:
    """The export's week runs Sunday to Saturday."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def calendar_problem(obs: Observation, now: datetime) -> str | None:
    """Why this export cannot be trusted to say "no event left", or None when it can.

    Fail closed (D-4): an empty export, an unknown impact label, an unparseable date on a row
    the blackout would use, or an export from another week all mean *unknown*.
    """
    return rows_problem(obs.data, now)


def rows_problem(rows: Any, now: datetime) -> str | None:
    """`calendar_problem` on the raw export, for `fetch_json(validate=...)`."""
    if not isinstance(rows, list) or not rows:
        return "calendar export is empty or not a list"
    if not all(isinstance(r, dict) for r in rows):
        return "calendar export has non-object rows"
    unknown = sorted({_impact(r) for r in rows} - KNOWN_IMPACTS)
    if unknown:
        return f"calendar impact labels changed: {unknown[:5]}"
    dated: list[datetime] = []
    for row in rows:
        at = _parse_at(row.get("date"))
        if at is None and _impact(row) == "high":
            return f"calendar date unparseable on a high-impact row: {row.get('date')!r}"
        local = _parse_local(row.get("date"))
        if local is not None:
            dated.append(local)
    if not dated:
        return "calendar export has no parseable dates"
    first = min(dated)
    local_now = now.astimezone(first.tzinfo)
    if _week_start(first.date()) != _week_start(local_now.date()):
        return f"calendar export is for the week of {_week_start(first.date())}, not this week"
    return None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def high_impact_events(
    obs: Observation, currencies: tuple[str, ...] = MARKET_MOVING
) -> list[dict[str, Any]]:
    """High-impact events for the given currencies, with UTC times; blanks stay None."""
    if not isinstance(obs.data, list):
        return []
    events: list[dict[str, Any]] = []
    for row in obs.data:
        if not isinstance(row, dict) or _impact(row) != "high":
            continue
        if str(row.get("country") or "").strip().upper() not in currencies:
            continue
        at = _parse_at(row.get("date"))
        if at is None:
            continue
        events.append(
            {
                "title": scrub(row.get("title")).text,
                "currency": row.get("country"),
                "at": at,
                "forecast": None if _blank(row.get("forecast")) else row["forecast"],
                "previous": None if _blank(row.get("previous")) else row["previous"],
            }
        )
    return sorted(events, key=lambda e: e["at"])


def events_within(
    events: list[dict[str, Any]], now: datetime, hours: float
) -> list[dict[str, Any]]:
    """Events starting in the next `hours` — the blackout window before a new entry."""
    return [e for e in events if 0 <= (e["at"] - now).total_seconds() <= hours * 3600]
