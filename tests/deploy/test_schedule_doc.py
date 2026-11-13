"""The schedule written down must be the schedule systemd runs.

`config/schedule.yaml` said the model eval ran weekly on Sunday 02:00 for weeks after the timer
became daily at 08:30 UTC, and RUNBOOK.md repeated it (F-098: a schedule and its description drift
apart with every deploy reporting success). Nothing reads schedule.yaml at run time — the timers
are the truth — so this test is the only thing keeping the two honest.
"""

from __future__ import annotations

import re

import yaml

from .systemd_units import REPO, unit_values

SCHEDULE = REPO / "config" / "schedule.yaml"


def _hhmm(unit: str) -> list[str]:
    """`HH:MM` for every daily firing of a `*-*-* H[,H…]:MM:SS UTC` OnCalendar line."""
    (spec,) = unit_values(unit, "OnCalendar")
    match = re.fullmatch(r"(?:\w{3} )?\*-\*-\* ([\d,]+):(\d\d):00 UTC", spec)
    assert match, f"{unit}: unexpected OnCalendar {spec!r}"
    return [f"{int(h):02d}:{match.group(2)}" for h in match.group(1).split(",")]


def _schedule() -> dict[str, object]:
    loaded = yaml.safe_load(SCHEDULE.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_schedule_yaml_lists_the_times_the_timers_fire() -> None:
    schedule = _schedule()
    assert schedule["review"] == _hhmm("argus-review.timer")
    assert schedule["manager"] == _hhmm("argus-manager.timer")
    assert schedule["eval_models"] == _hhmm("argus-eval-models.timer")[0]
    assert schedule["backup"] == _hhmm("argus-backup.timer")[0]


def test_the_weekly_reflection_is_written_with_its_weekday_and_hour() -> None:
    (spec,) = unit_values("argus-reflect.timer", "OnCalendar")
    assert spec.startswith("Sun ")
    assert _schedule()["reflect"] == f"Sun {_hhmm('argus-reflect.timer')[0]}"


def test_schedule_yaml_says_the_eval_is_daily_not_weekly() -> None:
    assert "Sun" not in str(_schedule()["eval_models"])
    assert unit_values("argus-eval-models.timer", "OnCalendar")[0].startswith("*-*-*")


def test_the_safety_reference_describes_the_timers_it_lists() -> None:
    text = (REPO / "docs" / "SAFETY.md").read_text()
    assert re.search(r"argus-eval-models\.timer` \(\*\*daily\*\* 08:30", text)
    assert re.search(r"argus-reflect\.timer` \(Sun 09:30\)", text)
    assert "(Sun 02:00)" not in text
    assert "argus-autodeploy.timer" in text
