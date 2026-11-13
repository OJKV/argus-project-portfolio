"""Which manager slots an account may run in (Q6, owner's grilling decisions of 2026-10-26).

`argus-manager.timer` fires six times a day. The live account keeps exactly its old schedule,
00:30 and 12:30 UTC; the four slots in between exist for the demo account's exploration profile.
The timer cannot tell the accounts apart, so the scheduled run checks itself here and a live run in
a demo-only slot exits 0 before it touches anything.

Only a run started with `--scheduled` (the systemd unit) is checked: a hand-run `argus manager` is
not a slot, and works at any time on either account.

The slot is read from the wall clock at process start, before the run lock is taken, so the lock
wait never counts. A demo-only slot's window starts EARLY minutes before it (clock skew) and lasts
LATE minutes after it — enough for systemd's AccuracySec (1 min), the 30 s randomised delay and a
slow start. Anything later is a `Persistent=true` catch-up after downtime (a reboot at 04:40, say)
and runs on live exactly as it did before the demo slots existed; only a start inside the narrow
window is refused.

09:30, not 08:30: the daily `argus-eval-models` run is at 08:30 and shares the provider limits.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

LIVE_SLOTS = (time(0, 30), time(12, 30))
# owner 2026-10-27: "keep 4h AI runs going forward for both demo and live" — no slot is demo-only
# any more, so the live account runs at all six times too. Put the four times back here to restore
# the old live schedule (00:30/12:30 only).
DEMO_ONLY_SLOTS: tuple[time, ...] = ()
EARLY = timedelta(minutes=10)
LATE = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC)


def demo_only_slot(moment: datetime) -> time | None:
    """The demo-only slot `moment` belongs to, or None (a live slot, or no slot at all)."""
    utc = moment.astimezone(UTC)
    for slot in DEMO_ONLY_SLOTS:
        at = datetime.combine(utc.date(), slot, tzinfo=UTC)
        if at - EARLY <= utc < at + LATE:
            return slot
    return None


def scheduled_run_refusal(venue: str, moment: datetime | None = None) -> str | None:
    """Why a scheduled manager run must not run now, or None. Only "demo" uses the extra slots."""
    slot = demo_only_slot(moment or _now())
    if slot is None or venue == "demo":
        return None
    return (
        f"{slot:%H:%M} UTC is a demo-only manager slot; the live account runs only at "
        "00:30/12:30 UTC. Nothing to do."
    )
