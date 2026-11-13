"""Token unlock schedules (FAILURE_LOG F-048 replacement).

`api.llama.fi/emissions` became paid in 2026, but the dataset CDN behind it still serves the
same schedules for free: `defillama-datasets.llama.fi/emissions/{slug}`. Each category carries a
daily *cumulative* unlocked series that extends years into the future, so the next unlock is the
next step up in the total.

Sizing is expressed against tokens already unlocked — the honest denominator available here —
never against a circulating supply we did not measure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "unlocks"
TIER: Tier = 1
HOST = "https://defillama-datasets.llama.fi"
MIN_STEP_FRACTION = 0.0005  # ignore rounding dust in the cumulative series
# A step this large (of tokens already unlocked) is a cliff, not daily emission. Reporting only:
# unlock data feeds the dossier, never a gate.
CLIFF_STEP_FRACTION = 0.005
CLIFF_HORIZON_DAYS = 30  # the largest unlock inside this window is reported beside the next


def fetch_catalogue() -> Observation:
    return fetch_json(f"{SOURCE}.catalogue", f"{HOST}/emissionsProtocolsList", TIER, "reference")


def fetch_schedule(slug: str) -> Observation:
    return fetch_json(f"{SOURCE}.{slug}", f"{HOST}/emissions/{slug}", TIER, "onchain")


def match_slug(name: str, catalogue: list[str]) -> str | None:
    """Exact slug, else a single unambiguous prefix match, else None.

    "arbitrum" matches both `arbitrum-foundation` and `arbitrum-exchange`; guessing which one
    holds the token schedule is the mapping-drift trap (DATA_SOURCES.md §9 #11). Ambiguity is
    refused and resolved by an explicit `unlocks_slug` in `knowledge/universe.yaml`.
    """
    target = name.lower()
    if target in catalogue:
        return target
    prefixed = [s for s in catalogue if s.startswith(target)]
    return prefixed[0] if len(prefixed) == 1 else None


def next_unlock(obs: Observation, now: datetime | None = None) -> dict[str, Any] | None:
    """The unlock that matters next: when, how much, for whom, and the emission around it.

    Each category's cumulative series is forward-filled onto every stamp before summing (D-14):
    a category reported monthly used to drop out of the daily totals and reappear as a fake
    cliff.

    The headline (`in_days`, `pct_of_unlocked`, `recipients`) is the next *cliff*, a step of at
    least `CLIFF_STEP_FRACTION`: the first step above dust made a linear daily emitter read
    "in 1 d, 0.08%" while a 4% cliff five days out never surfaced (D-14). It is the next cliff,
    not the largest one, because the largest hid a 2% cliff in 3 days behind a 4% one in 25,
    and turned a daily emitter whose step grows by one token into "in 30 d". With no cliff
    ahead, the next step above dust leads, so a pure emitter reads "in 1 d". The largest step
    inside `CLIFF_HORIZON_DAYS` is reported beside it (`max_30d_*`), and `pct_next_7d` /
    `pct_next_30d` carry the total emission over those windows.
    """
    if obs.quality != "ok" or not isinstance(obs.data, dict):
        return None
    categories = obs.data.get("documentedData", {}).get("data") or []
    if not categories:
        return None
    moment = now or datetime.now(UTC)
    cutoff = moment.timestamp()

    raw: dict[str, dict[int, float]] = {}
    for category in categories:
        label = str(category.get("label", "?"))
        series = raw.setdefault(label, {})
        for point in category.get("data") or []:
            try:
                series[int(point["timestamp"])] = float(point["unlocked"])
            except (KeyError, TypeError, ValueError):
                continue
    stamps = sorted({stamp for series in raw.values() for stamp in series})
    if not stamps:
        return None
    filled = {label: _forward_fill(series, stamps) for label, series in raw.items()}
    totals = [sum(values[i] for values in filled.values()) for i in range(len(stamps))]

    past = [i for i, stamp in enumerate(stamps) if stamp <= cutoff]
    now_index = past[-1] if past else -1
    unlocked_now = totals[now_index] if past else 0.0
    threshold = max(unlocked_now * MIN_STEP_FRACTION, 0.0)

    steps = [
        (i, totals[i] - (totals[i - 1] if i > 0 else 0.0))
        for i in range(now_index + 1, len(stamps))
    ]
    real = [(i, step) for i, step in steps if step > threshold]
    if not real:
        return None
    cliff_size = unlocked_now * CLIFF_STEP_FRACTION
    cliffs = [(i, step) for i, step in real if step >= cliff_size]
    index, step = cliffs[0] if cliffs else real[0]
    horizon = cutoff + CLIFF_HORIZON_DAYS * 86400
    near = [(i, step) for i, step in real if stamps[i] <= horizon]
    # max() keeps the first of equal steps, i.e. the earliest.
    largest = max(near, key=lambda pair: pair[1]) if near else None

    def pct(tokens: float) -> float | None:
        return tokens / unlocked_now * 100 if unlocked_now else None

    def days_to(i: int) -> int:
        return (datetime.fromtimestamp(stamps[i], UTC).date() - moment.date()).days

    def emitted_within(days: int) -> float:
        inside = [i for i in range(len(stamps)) if stamps[i] <= cutoff + days * 86400]
        return (totals[inside[-1]] if inside else 0.0) - unlocked_now

    when = datetime.fromtimestamp(stamps[index], UTC).date()
    return {
        "date": when,
        "in_days": (when - moment.date()).days,
        "tokens": step,
        "pct_of_unlocked": pct(step),
        "recipients": sorted(
            label
            for label, values in filled.items()
            if values[index] - (values[index - 1] if index > 0 else 0.0) > 0
        ),
        "unlocked_before": unlocked_now,
        "max_30d_in_days": days_to(largest[0]) if largest else None,
        "max_30d_pct": pct(largest[1]) if largest else None,
        "pct_next_7d": pct(emitted_within(7)),
        "pct_next_30d": pct(emitted_within(30)),
    }


def _forward_fill(series: dict[int, float], stamps: list[int]) -> list[float]:
    """Cumulative value at every stamp: the latest point at or before it, 0 before the first."""
    out: list[float] = []
    last = 0.0
    for stamp in stamps:
        last = series.get(stamp, last)
        out.append(last)
    return out
