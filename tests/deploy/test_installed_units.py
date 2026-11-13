"""Which timers a fresh install enables, asserted against the units that exist.

F-141: `argus-manager.timer` shipped in the repo and `install-units.sh` did not enable it. Nothing
compared the two lists, so the core of the system ran only when someone typed the command, for
eleven days, while the alert that said so went to a dead Telegram channel.

The point of these tests is that the drift is now a test failure rather than something you notice
by reading a shell script you have no reason to reread.
"""

from __future__ import annotations

import re

from .systemd_units import DEPLOY, unit_values

INSTALL = DEPLOY / "install-units.sh"

# Every timer the repo ships, and whether a fresh install should start it. Adding a timer without
# deciding this is exactly the omission F-141 was.
EXPECTED: dict[str, bool] = {
    "argus-watchdog.timer": True,
    "argus-backup.timer": True,
    "argus-eval-models.timer": True,
    "argus-reflect.timer": True,
    "argus-live-keys.timer": True,
    "argus-manager.timer": True,
    "argus-review.timer": True,
    # Follows deploy/green every five minutes. Enabled like the rest: a fresh box that is not
    # deploying is a box running yesterday's code, which is exactly what F-098 was.
    "argus-autodeploy.timer": True,
}


def _enabled_by_installer() -> set[str]:
    """The timer-enabling loop, picked out by content rather than position.

    The script has more than one `for unit in` loop (--smoke iterates its arguments), so matching
    the first one silently read the wrong list.
    """
    text = INSTALL.read_text()
    loops = [
        body.replace("\\\n", " ")
        for body in re.findall(r"for unit in (.*?); do", text, re.S)
        if ".timer" in body
    ]
    assert len(loops) == 1, f"expected exactly one timer-enabling loop, found {len(loops)}"
    return set(loops[0].split())


def test_every_shipped_timer_has_a_decision() -> None:
    shipped = {p.name for p in (DEPLOY / "systemd").glob("*.timer")}
    assert shipped == set(EXPECTED), (
        "a timer was added or removed without deciding whether a fresh install enables it"
    )


def test_the_installer_enables_exactly_the_timers_we_chose() -> None:
    assert _enabled_by_installer() == {name for name, on in EXPECTED.items() if on}


def test_the_manager_and_its_review_are_enabled_together() -> None:
    """A manager without a review opens positions that nothing then tends."""
    enabled = _enabled_by_installer()
    assert ("argus-manager.timer" in enabled) == ("argus-review.timer" in enabled)


def test_the_watchdog_is_enabled_whenever_the_manager_is() -> None:
    """The watchdog is what guarantees the exchange-side stop (non-negotiable #4)."""
    enabled = _enabled_by_installer()
    if "argus-manager.timer" in enabled:
        assert "argus-watchdog.timer" in enabled


# ── Schedule ──────────────────────────────────────────────────────────────────────────────────


def _calendar_minutes(name: str) -> set[int]:
    """Minutes past UTC midnight for a daily `*-*-* HH[,HH…]:MM:SS UTC` OnCalendar line."""
    (spec,) = unit_values(name, "OnCalendar")
    match = re.fullmatch(r"\*-\*-\* ([\d,]+):(\d\d):00 UTC", spec)
    assert match, f"{name}: unexpected OnCalendar {spec!r}"
    return {int(h) * 60 + int(match.group(2)) for h in match.group(1).split(",")}


def test_the_review_runs_on_the_hour_every_six_hours() -> None:
    """owner moved the review to 00/06/12/18:00 UTC (2026-10-07) so it never shares a slot with the
    manager: at :15, a review that had waited for the lock could still be running at :30."""
    assert _calendar_minutes("argus-review.timer") == {0, 6 * 60, 12 * 60, 18 * 60}


def test_the_manager_fires_six_times_a_day_at_half_past() -> None:
    """00:30/12:30 for both accounts; 04:30, 09:30, 16:30 and 20:30 are demo-only (Q6, 2026-10-26;
    09:30, not 08:30, to stay clear of the daily eval-models run)
    — a live run exits 0 there (`argus.runs.schedule`)."""
    assert _calendar_minutes("argus-manager.timer") == {h * 60 + 30 for h in (0, 4, 9, 12, 16, 20)}


def test_every_manager_run_has_a_review_at_least_thirty_minutes_before_it() -> None:
    """The review tends what exists before the manager adds to it, and must be done by then."""
    reviews = _calendar_minutes("argus-review.timer")
    for start in _calendar_minutes("argus-manager.timer"):
        earlier = [r for r in reviews if r <= start]
        assert earlier, f"no review before the manager run at minute {start}"
        assert start - max(earlier) >= 30


def test_the_watchdog_checks_stops_soon_after_a_reboot() -> None:
    """P-11: with no OnBootSec and Persistent=false, the first stop check after a reboot waited
    for the next :00/:20/:40 tick — up to twenty minutes with open positions and nobody looking."""
    assert unit_values("argus-watchdog.timer", "OnBootSec") == ["2min"]
    assert unit_values("argus-watchdog.timer", "Persistent") == ["true"]


def test_a_backup_missed_while_the_box_was_down_runs_at_boot() -> None:
    assert unit_values("argus-backup.timer", "Persistent") == ["true"]


def test_the_weekly_key_check_fires_clear_of_the_on_the_hour_runs() -> None:
    """At Mon 06:00 the live-keys unit queued behind the review AND the watchdog's :00 tick, and
    its `flock -w 300` could time out and send a false failure alert. Its whole randomised window
    now sits between the :00 runs and the :20 watchdog tick."""
    (spec,) = unit_values("argus-live-keys.timer", "OnCalendar")
    match = re.fullmatch(r"Mon \*-\*-\* 06:(\d\d):00 UTC", spec)
    assert match, spec
    start = int(match.group(1)) * 60
    (delay,) = unit_values("argus-live-keys.timer", "RandomizedDelaySec")
    assert start >= 5 * 60, "fires with the :00 review and watchdog tick"
    assert start + int(delay) <= 15 * 60, "can still be waiting when the :20 watchdog tick fires"
