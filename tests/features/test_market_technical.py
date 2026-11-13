"""What the technical family says about when its price was true."""

from __future__ import annotations

import pandas as pd
import pytest

from argus.data.envelope import Observation
from argus.features import indicators, market
from argus.features.universe import Asset

H4_MS = 4 * 3600 * 1000


def test_the_4h_price_carries_the_time_its_bar_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A close alone does not say how old it is, so a lagging feed or a cached frame passed for
    fresh and only a missing price was ever flagged stale (second review, D-N1 / L-3). The newest
    closed bar opened 4 h before the forming one, so its close was true when the forming one
    opened."""
    forming = 1_789_574_400_000  # 2026-09-16T16:00Z
    rows = [  # OKX: newest first; confirm "0" = still forming
        [str(forming - i * H4_MS), "100", "101", "99", str(100 + i), "10", "10", "1000"]
        + ["0" if i == 0 else "1"]
        for i in range(60)
    ]

    def candles(_inst: str, bar: str, _limit: int = 300) -> tuple[pd.DataFrame, Observation]:
        obs = Observation(source="okx.public.candles", tier=1)
        return indicators.candles_to_frame(rows if bar == "4H" else []), obs

    monkeypatch.setattr(market, "candles", candles)
    btc_daily = indicators.candles_to_frame([])
    out, _ = market.technical(Asset("SOL", "SOL-USDT-SWAP", "", "satellite"), btc_daily)

    assert out["price"] == 101.0
    assert out["price_at"] == "2026-09-16T16:00:00+00:00"
