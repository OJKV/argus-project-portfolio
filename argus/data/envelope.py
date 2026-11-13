"""Observation envelope — every fetcher returns this (ARCHITECTURE.md §4)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

Tier = Literal[1, 2, 3]
Quality = Literal["ok", "stale", "suspect", "missing"]


def finite_number(value: Any) -> float | None:
    """`value` as a finite float, or None. Rejects bool (True would read as 1.0), NaN and inf.

    The one parser for untrusted numbers: a blank, bool or non-finite value is a gap, not data.
    """
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class Observation(BaseModel):
    source: str
    tier: Tier
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    asof: datetime | None = None
    quality: Quality = "ok"
    notes: list[str] = Field(default_factory=list)
    data: dict[str, Any] | list[Any] | None = None

    def age(self, now: datetime | None = None) -> timedelta | None:
        if self.asof is None:
            return None
        return (now or datetime.now(UTC)) - self.asof

    def mark_stale(self, max_age: timedelta, now: datetime | None = None) -> Observation:
        age = self.age(now)
        if age is not None and age > max_age and self.quality == "ok":
            return self.model_copy(
                update={"quality": "stale", "notes": [*self.notes, f"age {age} > {max_age}"]}
            )
        return self

    @classmethod
    def missing(cls, source: str, tier: Tier, note: str) -> Observation:
        return cls(source=source, tier=tier, quality="missing", notes=[note], data=None)
