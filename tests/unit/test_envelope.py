from datetime import UTC, datetime, timedelta

from argus.data.envelope import Observation


def test_stale_marking() -> None:
    now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    o = Observation(
        source="farside.btc", tier=1, asof=now - timedelta(hours=30), data={"flow": None}
    )
    assert o.mark_stale(timedelta(hours=24), now).quality == "stale"
    assert o.mark_stale(timedelta(hours=48), now).quality == "ok"


def test_missing_is_not_zero() -> None:
    o = Observation.missing("farside.btc", 1, "blank cell → missing, not zero (trap #3)")
    assert o.quality == "missing" and o.data is None
