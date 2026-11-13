"""Alert rules (OBSERVABILITY_AND_GUARDRAILS.md §1).

The rules are pure functions over facts so they can be tested without a clock, a database or a
network, and so the same rule can be evaluated inside a run *and* from `argus doctor`. Delivery
is separate and best-effort: an alert that cannot be sent is still logged and still journaled by
the caller.

`positions_unprotected` is the one that matters most — it is the metric behind "every position
must have an exchange-side stop" (AGENTS.md rule 4). It must always read 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import Engine, text

from argus.observability import metrics

log = structlog.get_logger(__name__)

Severity = Literal["warning", "critical"]


@dataclass(frozen=True)
class Alert:
    key: str
    severity: Severity
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.key}: {self.message}"


def run_over_budget(kind: str, seconds: float, budget_s: float) -> Alert | None:
    """A run that outgrows its budget will eventually collide with the next schedule."""
    if budget_s <= 0 or seconds <= budget_s:
        return None
    return Alert(
        "run_over_budget",
        "warning",
        f"{kind} run took {seconds:.0f}s, budget {budget_s:.0f}s",
    )


def unprotected_positions(count: int) -> Alert | None:
    if count <= 0:
        return None
    return Alert(
        "positions_unprotected",
        "critical",
        f"{count} open position(s) without an exchange-side stop",
    )


def breaker_open(hosts: list[str]) -> Alert | None:
    """Critical, so it repeats hourly. While the venue cannot be reached, stop verification and the
    drawdown kill-switch are both blind, and this is the only Telegram signal: each watchdog tick is
    a fresh process, so the in-memory breaker never reaches the count `exchange_unreachable` needs.
    """
    if not hosts:
        return None
    return Alert("breaker_open", "critical", f"circuit breaker open: {', '.join(sorted(hosts))}")


def llm_error_rate(calls: int, failures: int, threshold: float = 0.2) -> Alert | None:
    """Below 10 calls the ratio is noise, so it is not worth waking anyone."""
    if calls < 10:
        return None
    rate = failures / calls
    if rate <= threshold:
        return None
    return Alert("llm_error_rate", "warning", f"LLM failures {rate:.0%} of {calls} calls")


def stale_manager(last_run: datetime | None, now: datetime, max_gap_h: float) -> Alert | None:
    """Silence is the failure mode nobody notices: no run at all raises no other alert."""
    if last_run is None:
        return Alert("manager_stale", "warning", "no manager run on record")
    gap = now - last_run
    if gap <= timedelta(hours=max_gap_h):
        return None
    return Alert(
        "manager_stale",
        "warning",
        f"last manager run {gap.total_seconds() / 3600:.1f}h ago (limit {max_gap_h:.0f}h)",
    )


def agent_failed(agent: str, criticality: Any, symbol: str, detail: str) -> Alert | None:
    """An agent could not answer. Whether that matters depends on what the agent was for.

    The detail is deliberately summarised, not quoted: provider errors echo the request, and a
    Telegram message is not where anyone should discover what a URL contained (F-070).
    """
    level = getattr(criticality, "value", str(criticality))
    if level == "optional":
        log.info("agent.failed_optional", agent=agent, symbol=symbol)
        return None
    consequence = (
        "no signal for this asset"
        if level == "required"
        else "the check did not run, so the asset was NOT traded"
    )
    return Alert(
        f"agent_failed_{agent}",
        "critical",
        f"{agent} failed on {symbol}: {consequence} ({_summarise(detail)})",
    )


def _summarise(detail: str) -> str:
    """First line, no query strings, short enough to read on a phone."""
    head = detail.split("\n", 1)[0]
    head = re.sub(r"[?&][^\s]*", "", head)
    return head[:110]


def run_failed(kind: str, run_id: str, error: str) -> Alert:
    """A run that failed is not a quiet run. The watchdog is the safety net, so its is critical."""
    return Alert(
        f"run_failed_{kind}",
        "critical" if kind == "watchdog" else "warning",
        f"{kind} run {run_id[:8]} FAILED: {_summarise(error)}",
    )


def runs_abandoned(ghosts: list[tuple[str, str]]) -> Alert | None:
    """Runs whose process died mid-run — systemd's SIGTERM at TimeoutStartSec, an OOM kill — and
    were reaped from RUNNING to ABANDONED. Nothing else reports them: the process that would have
    raised `run_failed` is the one that died."""
    if not ghosts:
        return None
    listed = ", ".join(f"{kind} {run_id[:8]}" for run_id, kind in ghosts)
    return Alert("run_abandoned", "warning", f"died while RUNNING, now ABANDONED: {listed}")


def watchdog_skipped(consecutive: int) -> Alert | None:
    """One skipped tick is routine — the next is 20 minutes away. Two in a row is a gap (J-4)."""
    if consecutive < 2:
        return None
    return Alert(
        "watchdog_skipped",
        "critical",
        f"{consecutive} watchdog ticks in a row skipped: another run holds the lock, so no "
        "position has been verified since the last completed tick",
    )


def last_run_at(engine: Engine, kind: str = "manager", *, venue: str) -> datetime | None:
    """When the last run of `kind` finished OK.

    Only OK counts. A FAILED, HALTED, ABANDONED or still-RUNNING row looked at nothing, and
    reading MAX(started_at) over any status let a run that died on a dead VPN reset the stale
    clock — "OK, 0 dossiers", green everywhere (J-1).

    Only `venue`'s runs (F-150): a demo manager run is not the live manager running, and counting
    it kept the live stale-manager alert quiet.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT MAX(started_at) FROM runs"
                " WHERE kind = :k AND status = 'OK' AND config_hash = :venue"
            ),
            {"k": kind, "venue": venue},
        ).scalar()
    if not row:
        return None
    try:
        return datetime.fromisoformat(str(row))
    except ValueError:
        return None


# How long a key that has reached the operator stays quiet while it remains true (review O-3).
# A condition that persists is a reminder at this rate, not a message every 20-minute tick: the
# stale-manager alert fired 364 times into a dead channel (F-141), and a channel that repeats
# itself 72 times a day is the one that gets muted before the P1 arrives. Critical repeats sooner.
# Operational pacing, not a risk threshold. Per-event P1 messages (`Notifier.repair_failed`) do
# not pass through here and are never held back.
COOLDOWN = {"critical": timedelta(hours=1), "warning": timedelta(hours=6)}


def dispatch(
    alerts: list[Alert | None],
    notifier: object | None = None,
    engine: Engine | None = None,
    now: datetime | None = None,
) -> list[Alert]:
    """Log, count and (best effort) send. Never raises — alerting is not on the trading path.

    With `engine`, each key is sent at most once per its COOLDOWN, tracked in `alert_log`. Every
    occurrence is still logged and counted; only the message is held back. The cooldown starts at
    a delivery, so a send the channel refused is retried on the next call rather than swallowed.
    """
    live = [a for a in alerts if a is not None]
    for alert in live:
        logger = log.error if alert.severity == "critical" else log.warning
        logger("alert", key=alert.key, severity=alert.severity, detail=alert.message)
        metrics.count("argus.alerts", key=alert.key, severity=alert.severity)
    if not live:
        return live
    moment = now or datetime.now(UTC)
    due = live if engine is None else _due(engine, live, moment)
    if not due:
        return live
    try:
        if notifier is None:
            from argus.notify.telegram import Notifier

            notifier = Notifier.from_settings()
        send = getattr(notifier, "send", None)
        if callable(send):
            delivered = send("\n".join(str(a) for a in due))
            if engine is not None and delivered is not False:
                _record_sent(engine, due, moment)
    except Exception as exc:  # noqa: BLE001 - a failed alert must not fail the run
        log.warning("alert.send_failed", error=f"{type(exc).__name__}: {exc}")
    return live


def _due(engine: Engine, live: list[Alert], now: datetime) -> list[Alert]:
    """The alerts whose key is not cooling down. On any journal error, all of them."""
    try:
        with engine.begin() as conn:
            sent = {
                r[0]: r[1]
                for r in conn.execute(text("SELECT key, last_sent_at FROM alert_log")).all()
            }
            due: list[Alert] = []
            for alert in live:
                last = sent.get(alert.key)
                if last is None or now - datetime.fromisoformat(last) >= COOLDOWN[alert.severity]:
                    due.append(alert)
                    continue
                conn.execute(
                    text("UPDATE alert_log SET suppressed = suppressed + 1 WHERE key = :k"),
                    {"k": alert.key},
                )
                log.info("alert.suppressed", key=alert.key, last_sent_at=last)
            return due
    except Exception as exc:  # noqa: BLE001 - a broken cooldown must not silence an alert
        log.warning("alert.cooldown_unavailable", error=f"{type(exc).__name__}: {exc}")
        return live


def _record_sent(engine: Engine, sent: list[Alert], now: datetime) -> None:
    try:
        with engine.begin() as conn:
            for alert in sent:
                conn.execute(
                    text(
                        "INSERT INTO alert_log (key, severity, last_sent_at, last_message)"
                        " VALUES (:k, :s, :t, :m) ON CONFLICT(key) DO UPDATE SET"
                        " severity = :s, last_sent_at = :t, last_message = :m, suppressed = 0"
                    ),
                    {
                        "k": alert.key,
                        "s": alert.severity,
                        "t": now.isoformat(),
                        "m": alert.message[:500],
                    },
                )
    except Exception as exc:  # noqa: BLE001 - worst case the next tick repeats the alert
        log.warning("alert.cooldown_unrecorded", error=f"{type(exc).__name__}: {exc}")
