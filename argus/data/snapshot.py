"""Raw market snapshot: the technical + derivatives families with provenance (Phase 2a).

This is the *data* half of a dossier. The compacted, evidence-tagged, ≤1.5k-token dossier the
LLM actually reads is built on top of this in Phase 2c (`features/dossier.py`) — keeping them
apart means the expensive-to-change prompt format never dictates how data is fetched.

Nothing here raises. A missing family becomes a `data_gaps` entry, which the Risk Gate reads
as a reason to shrink or veto (DECISION_LOGIC.md §6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from argus.config import KNOWLEDGE_DIR, load_yaml, universe_config
from argus.data.consensus import ConsensusPrice, consensus_price
from argus.data.envelope import Observation
from argus.data.fetchers import (
    binance_futures_public,
    bybit_kz_public,
    hyperliquid,
    okx_public,
    okx_rubik,
)
from argus.data.quality import DataQualityEvent, check_price_within_24h_range

TRADE_CRITICAL = ("technical", "derivatives")


@dataclass
class MarketSnapshot:
    symbol: str
    observed_at: datetime
    technical: dict[str, Any] = field(default_factory=dict)
    derivatives: dict[str, Any] = field(default_factory=dict)
    provenance: list[Observation] = field(default_factory=list)
    consensus: ConsensusPrice | None = None
    quality_events: list[DataQualityEvent] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)

    @property
    def tradable(self) -> bool:
        """Trade-critical families present, and enough sources agree on the price."""
        return (
            not any(f in gap for gap in self.data_gaps for f in TRADE_CRITICAL)
            and self.consensus is not None
            and self.consensus.trade_critical_ok
        )


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _listed_assets() -> list[dict[str, Any]]:
    """Core plus whitelist — every asset whose slugs have been verified by hand."""
    whitelist = load_yaml("whitelist.yaml", KNOWLEDGE_DIR).get("entries") or []
    return [*universe_config().get("core", []), *whitelist]


def symbol_mapping(symbol: str) -> dict[str, str]:
    """Venue-specific ids for one universe symbol. Trap 11 lives here: mappings drift."""
    for asset in _listed_assets():
        if asset["symbol"] == symbol.upper():
            return {
                "okx": asset["okx"],
                "bybit": f"{symbol.upper()}USDT",
                "binance": f"{symbol.upper()}USDT",
                "hyperliquid": symbol.upper(),
                "coingecko": asset.get("coingecko", ""),
            }
    return {
        "okx": f"{symbol.upper()}-USDT-SWAP",
        "bybit": f"{symbol.upper()}USDT",
        "binance": f"{symbol.upper()}USDT",
        "hyperliquid": symbol.upper(),
        "coingecko": "",
    }


def _rows(obs: Observation) -> list[Any]:
    payload = obs.data
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("result", {}).get("list") or []
        return list(rows)
    if isinstance(payload, list):
        return payload
    return []


def build_snapshot(symbol: str) -> MarketSnapshot:
    ids = symbol_mapping(symbol)
    snap = MarketSnapshot(symbol=symbol.upper(), observed_at=datetime.now(UTC))

    ticker = okx_public.fetch_ticker(ids["okx"])
    candles = okx_public.fetch_candles(ids["okx"], "4H", 100)
    funding = okx_public.fetch_funding(ids["okx"])
    open_interest = okx_public.fetch_open_interest(ids["okx"])
    ls_ratio = okx_rubik.fetch_long_short_ratio(symbol.upper())
    bybit_ticker = bybit_kz_public.fetch_ticker(ids["bybit"])
    binance_premium = binance_futures_public.fetch_premium_index(ids["binance"])
    hl = hyperliquid.fetch_meta_and_contexts()
    snap.provenance = [
        ticker,
        candles,
        funding,
        open_interest,
        ls_ratio,
        bybit_ticker,
        binance_premium,
        hl,
    ]

    # --- technical ---
    okx_row = _rows(ticker)[0] if _rows(ticker) else None
    if okx_row:
        last, high, low = (
            _decimal(okx_row.get("last")),
            _decimal(okx_row.get("high24h")),
            _decimal(okx_row.get("low24h")),
        )
        snap.technical = {
            "last": last,
            "high_24h": high,
            "low_24h": low,
            "bid": _decimal(okx_row.get("bidPx")),
            "ask": _decimal(okx_row.get("askPx")),
            "turnover_24h_usd": (
                (_decimal(okx_row.get("volCcy24h")) or Decimal(0)) * (last or Decimal(0))
            ),
            "candles_4h": len(_rows(candles)),
        }
        if last and high and low:
            try:  # trap 2 — a price outside its own range means something is stale
                check_price_within_24h_range(last, low, high, ticker.source, snap.symbol)
            except DataQualityEvent as event:
                snap.quality_events.append(event)
    else:
        snap.data_gaps.append("technical: no OKX ticker")
    if not _rows(candles):
        snap.data_gaps.append("technical: no OHLCV")

    # --- derivatives ---
    funding_row = _rows(funding)[0] if _rows(funding) else None
    oi_row = _rows(open_interest)[0] if _rows(open_interest) else None
    hl_ctx = hyperliquid.asset_context(hl, ids["hyperliquid"])
    if funding_row or oi_row:
        snap.derivatives = {
            "funding_rate_8h": _decimal(funding_row.get("fundingRate")) if funding_row else None,
            "next_funding_rate": (
                _decimal(funding_row.get("nextFundingRate")) if funding_row else None
            ),
            "open_interest_ccy": _decimal(oi_row.get("oiCcy")) if oi_row else None,
            "long_short_ratio": (_rows(ls_ratio)[0][1] if _rows(ls_ratio) else None),
            "hyperliquid_funding": _decimal(hl_ctx.get("funding")) if hl_ctx else None,
        }
    else:
        snap.data_gaps.append("derivatives: no funding or open interest")

    # --- price consensus across venues (F-010) ---
    prices: dict[str, Decimal] = {}
    if okx_row and (value := _decimal(okx_row.get("last"))):
        prices["okx.public.ticker"] = value
    bybit_rows = _rows(bybit_ticker)
    if bybit_rows and (value := _decimal(bybit_rows[0].get("lastPrice"))):
        prices["bybit.kz.public"] = value
    premium = binance_premium.data
    if isinstance(premium, dict) and (value := _decimal(premium.get("markPrice"))):
        prices["binance.futures.public"] = value
    if hl_ctx and (value := _decimal(hl_ctx.get("markPx"))):
        prices["hyperliquid"] = value
    snap.consensus = consensus_price(snap.symbol, prices)
    if snap.consensus.event is not None:
        snap.quality_events.append(snap.consensus.event)

    for obs in snap.provenance:
        if obs.quality == "missing":
            snap.data_gaps.append(f"{obs.source}: {obs.notes[0] if obs.notes else 'missing'}")
    return snap
