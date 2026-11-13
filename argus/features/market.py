"""Live assembly: fetch → indicators → regime snapshot and per-asset dossiers (Phase 2c).

The pure pieces (`indicators`, `regime.classify`, `dossier.assemble`, `asset_memory.build_card`)
are tested offline; this module only wires fetchers to them. Every fetch degrades to a gap, so a
dead source shrinks the dossier instead of failing the run.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any

import pandas as pd
import structlog
from sqlalchemy import Engine

from argus.config import KNOWLEDGE_DIR, get_settings, load_yaml
from argus.data.consensus import consensus_price
from argus.data.envelope import Observation, finite_number
from argus.data.fetchers import (
    binance_futures_public,
    bybit_kz_public,
    defillama,
    farside,
    fear_greed,
    forexfactory,
    fred,
    hyperliquid,
    news_rss,
    okx_public,
    okx_rubik,
    sec_edgar,
    unlocks,
)
from argus.data.http import fetch_json
from argus.data.parallel import run_parallel
from argus.features import asset_memory, indicators
from argus.features.dossier import Dossier, assemble, mentions
from argus.features.regime import RegimeInputs, RegimeView, breadth_pct, classify, hours_until
from argus.features.universe import Asset
from argus.gate.fees import FeeRates
from argus.gate.limits import LIVE_CONSERVATIVE, DecisionLimits
from argus.gate.rules import MarketContext
from argus.gate.sizing import InstrumentSpec

log = structlog.get_logger(__name__)


def _frame(obs: Observation) -> pd.DataFrame:
    rows = obs.data.get("data") if isinstance(obs.data, dict) else None
    return indicators.candles_to_frame(rows or [])


def candles(inst_id: str, bar: str, limit: int = 300) -> tuple[pd.DataFrame, Observation]:
    obs = okx_public.fetch_candles(inst_id, bar, limit)
    return _frame(obs), obs


def _rows(obs: Observation) -> list[Any]:
    return list(obs.data.get("data") or []) if isinstance(obs.data, dict) else []


def _pct(new: float, old: float) -> float | None:
    return (new / old - 1) * 100 if old else None


def _series_change(rows: list[Any], index: int, back: int) -> float | None:
    """Rubik rows are newest-first `[ts, value, …]`; change of `value` over `back` rows."""
    if len(rows) <= back:
        return None
    try:
        return _pct(float(rows[0][index]), float(rows[back][index]))
    except (TypeError, ValueError, IndexError):
        return None


def _frames(result: Any, inst_id: str, bar: str) -> tuple[pd.DataFrame, Observation]:
    """Unpack a parallel candle fetch, degrading to an empty frame on failure."""
    if isinstance(result, tuple):
        return result
    note = f"{type(result).__name__}: {result}" if isinstance(result, BaseException) else "no data"
    return pd.DataFrame(), Observation.missing(f"okx.public.candles.{bar}", 1, note)


def _unpack(result: Any, default: tuple[Any, ...]) -> Any:
    """A family that failed contributes nothing and is reported as a gap by the dossier."""
    if isinstance(result, BaseException):
        log.warning("family.failed", error=f"{type(result).__name__}: {result}")
        return default
    return result


def _number(value: Any) -> float | None:
    return finite_number(value)


def funding_percentile(current: Any, rates: list[float]) -> float | None:
    """Where `current` sits among `rates`, 0-100; None when either side is unusable."""
    now = finite_number(current)
    usable = [r for r in rates if finite_number(r) is not None]
    if now is None or not usable:
        return None
    return sum(r <= now for r in usable) / len(usable) * 100


def load_catalysts() -> list[dict[str, Any]]:
    return list(load_yaml("catalysts.yaml", KNOWLEDGE_DIR).get("items") or [])


def load_tokenomics() -> dict[str, Any]:
    return dict(load_yaml("tokenomics.yaml", KNOWLEDGE_DIR).get("assets") or {})


# --- regime -------------------------------------------------------------------------------


def regime_snapshot(
    assets: list[Asset], now: datetime | None = None, limits: DecisionLimits = LIVE_CONSERVATIVE
) -> tuple[RegimeView, list[Observation]]:
    moment = now or datetime.now(UTC)
    prov: list[Observation] = []
    inputs = RegimeInputs()

    btc, btc_obs = candles("BTC-USDT-SWAP", "1D", 300)
    prov.append(btc_obs)
    stack = indicators.ema_stack(btc)
    if stack:
        inputs.btc_close = stack["close"]
        inputs.btc_ema20 = stack["ema20"]
        inputs.btc_ema50 = stack["ema50"]
        inputs.btc_ema20_slope = stack["slope20"]
    inputs.btc_rv20_pct = indicators.realized_vol_percentile(btc, window=20, lookback=365)

    above: list[bool] = []
    fundings: list[float] = []
    for asset in assets:
        frame, _ = candles(asset.okx, "1D", 60)
        if len(frame) >= 20:
            ema20 = frame["close"].ewm(span=20, adjust=False).mean().iloc[-1]
            above.append(bool(frame["close"].iloc[-1] > ema20))
        rows = _rows(okx_public.fetch_funding(asset.okx))
        rate = _number(rows[0].get("fundingRate")) if rows and isinstance(rows[0], dict) else None
        if rate is not None:
            fundings.append(rate)
    inputs.breadth_20 = breadth_pct(above) if len(above) >= max(3, len(assets) // 2) else None
    inputs.funding_median = statistics.median(fundings) if fundings else None

    oi = okx_rubik.fetch_open_interest_volume("BTC", "1D")
    prov.append(oi)
    inputs.oi_7d_pct = _series_change(_rows(oi), 1, 7)

    stables = defillama.fetch_stablecoin_supply()
    prov.append(stables)
    if isinstance(stables.data, list) and len(stables.data) > 8:
        try:
            last = float(stables.data[-1]["totalCirculatingUSD"]["peggedUSD"])
            week = float(stables.data[-8]["totalCirculatingUSD"]["peggedUSD"])
            inputs.stable_supply_7d_pct = _pct(last, week)
        except (KeyError, TypeError, ValueError):
            pass

    flows = farside.fetch_btc_flows()
    prov.append(flows)
    if flows.quality == "ok":
        net, reporting_days = farside.net_flow(farside.parse_flows(flows), 5, moment.date())
        if net is not None:
            inputs.etf_5d_usd = net * 1e6  # Farside reports US$ millions
            if reporting_days < 3:
                view_note = f"ETF flow covers only {reporting_days} reporting day(s)"
                prov.append(Observation.missing("farside.coverage", 1, view_note))

    real_yield = fred.fetch_series(fred.SERIES["real_yield_10y"])
    prov.append(real_yield)
    inputs.real_yield_10y, _ = fred.latest_value(real_yield)

    fng = fear_greed.fetch_fear_greed(1)
    prov.append(fng)
    fng_rows = _rows(fng)
    if fng_rows and isinstance(fng_rows[0], dict):
        # One malformed reading is a gap, not an abort of the whole regime (D-N6).
        inputs.fear_greed = _number(fng_rows[0].get("value"))

    calendar = forexfactory.fetch_calendar(moment)
    prov.append(calendar)
    if calendar.quality == "ok":
        upcoming = [e for e in forexfactory.high_impact_events(calendar) if e["at"] >= moment]
        if upcoming:
            inputs.next_event_hours = hours_until(upcoming[0]["at"], moment)
            inputs.next_event = upcoming[0]["title"]
        else:
            # The export covers this week only: nothing left this week is still a known answer.
            inputs.next_event_hours = 168.0
            inputs.next_event = "none left this week"

    return classify(inputs, limits), prov


# --- per-asset families -------------------------------------------------------------------


def technical(asset: Asset, btc_daily: pd.DataFrame) -> tuple[dict[str, Any], list[Observation]]:
    bars = run_parallel(
        {
            "1D": lambda: candles(asset.okx, "1D", 300),
            "4H": lambda: candles(asset.okx, "4H", 300),
            "1H": lambda: candles(asset.okx, "1H", 300),
        },
        workers=3,
    )
    (daily, d_obs) = _frames(bars["1D"], asset.okx, "1D")
    (four, f_obs) = _frames(bars["4H"], asset.okx, "4H")
    (hourly, h_obs) = _frames(bars["1H"], asset.okx, "1H")
    out: dict[str, Any] = {}
    if not four.empty:
        out["price"] = float(four["close"].iloc[-1])
        # When that close was true: its bar's open plus 4 h. Not rendered (not in TECH_FIELDS);
        # the review reads it to flag a lagging feed or a cached frame as stale (D-N1 / L-3).
        out["price_at"] = (four.index[-1] + pd.Timedelta(hours=4)).isoformat()
    out["ema_1d"] = indicators.ema_stack(daily)
    out["ema_4h"] = indicators.ema_stack(four)
    out["ema_1h"] = indicators.ema_stack(hourly)
    out["rsi_4h"] = indicators.rsi(four)
    out["adx_4h"] = indicators.adx(four)
    atr = indicators.atr(four)
    if atr:
        out["atr_4h"], out["atr_4h_pct"] = atr["atr"], atr["atr_pct"]
    out["bb_pos_4h"] = indicators.bollinger_position(four)
    for key, value in indicators.swing_structure(four).items():
        out[f"{key}_4h"] = value
    out["vwap_1d"] = indicators.vwap(hourly, bars=24)
    out["rel_vol_4h"] = indicators.relative_volume(four)
    rets = indicators.period_returns(daily, (7, 30))
    out["ret_7d"], out["ret_30d"] = rets.get("ret_7"), rets.get("ret_30")
    out["beta_btc_30d"] = indicators.beta(daily, btc_daily, window=30)
    out["rv20_pct"] = indicators.realized_vol_percentile(daily)
    return out, [d_obs, f_obs, h_obs]


def funding_interval_hours(row: dict[str, Any]) -> float | None:
    """Hours between this instrument's funding settlements, from OKX's own schedule (G-9).

    `nextFundingTime - fundingTime` is the interval OKX applies; many alts settle every 4, 2 or
    1 h, not 8. None when the venue did not say — the gate rejects rather than assume.
    """
    start, end = _number(row.get("fundingTime")), _number(row.get("nextFundingTime"))
    if start is None or end is None or end <= start:
        return None
    return (end - start) / 3_600_000


def derivatives(
    asset: Asset, hl: Observation
) -> tuple[dict[str, Any], list[Observation], list[float]]:
    """Derivatives family, its provenance, and the funding history (oldest first) for memory.

    Deliberately does not call `build_snapshot`: that re-fetched candles and a ticker the
    technical family already has (F-059). Price consensus is not computed here: it travels with
    the live mark in `live_quote`, which is read again when the plan is gated (review D-2).
    """
    fetched = run_parallel(
        {
            "funding": lambda: okx_public.fetch_funding(asset.okx),
            "oi": lambda: okx_public.fetch_open_interest(asset.okx),
            "hist": lambda: okx_public.fetch_funding_history(asset.okx, 90),
            "oi_1h": lambda: okx_rubik.fetch_open_interest_volume(asset.symbol, "1H"),
            "oi_1d": lambda: okx_rubik.fetch_open_interest_volume(asset.symbol, "1D"),
            "ls": lambda: okx_rubik.fetch_long_short_ratio(asset.symbol),
            "taker": lambda: okx_rubik.fetch_taker_volume(asset.symbol),
        },
        workers=6,
    )
    missing = Observation.missing(f"okx.{asset.symbol}", 1, "parallel fetch failed")
    obs = {k: (v if isinstance(v, Observation) else missing) for k, v in fetched.items()}

    funding_row = _rows(obs["funding"])[0] if _rows(obs["funding"]) else {}
    oi_row = _rows(obs["oi"])[0] if _rows(obs["oi"]) else {}
    ls_rows = _rows(obs["ls"])
    out: dict[str, Any] = {
        "funding_8h": _number(funding_row.get("fundingRate")),
        "next_funding_8h": _number(funding_row.get("nextFundingRate")),
        "funding_interval_h": funding_interval_hours(funding_row),
        "open_interest_ccy": _number(oi_row.get("oiCcy")),
        "ls_ratio": _number(ls_rows[0][1]) if ls_rows else None,
    }
    # One unparseable settlement is skipped, not raised: a raise here made `_unpack` drop the
    # whole family, funding_8h included (the per-asset twin of D-18g).
    parsed = [_number(r.get("fundingRate")) for r in _rows(obs["hist"]) if isinstance(r, dict)]
    rates = [r for r in parsed if r is not None]
    if rates:
        out["funding_avg_7d"] = statistics.fmean(rates[:21])  # 3 per day, newest first
        out["funding_pctile_30d"] = funding_percentile(out["funding_8h"], rates)
    out["oi_24h_pct"] = _series_change(_rows(obs["oi_1h"]), 1, 24)
    out["oi_7d_pct"] = _series_change(_rows(obs["oi_1d"]), 1, 7)
    taker_rows = _rows(obs["taker"])[:24]
    try:
        sells = sum(float(r[1]) for r in taker_rows)
        buys = sum(float(r[2]) for r in taker_rows)
        out["taker_ratio"] = buys / sells if sells else None
    except (TypeError, ValueError, IndexError):
        pass
    ctx = hyperliquid.asset_context(hl, asset.symbol)
    hl_rate = _number(ctx.get("funding")) if ctx else None
    if hl_rate is not None:
        out["hl_funding"] = hl_rate * 8  # HL funds hourly; compare per 8h
    cleaned = {k: (float(v) if v is not None else None) for k, v in out.items()}
    return cleaned, list(obs.values()), list(reversed(rates))


def onchain(asset: Asset, tokenomics: dict[str, Any]) -> tuple[dict[str, Any], list[Observation]]:
    out: dict[str, Any] = {}
    prov: list[Observation] = []
    llama = asset.defillama or {}
    kind, slug = llama.get("type"), llama.get("slug")
    if kind == "chain" and slug:
        tvl = defillama.fetch_chain_tvl_history(slug)
        prov.append(tvl)
        if isinstance(tvl.data, list) and tvl.data:
            out["tvl_usd"] = tvl.data[-1].get("tvl")
            out["tvl_7d_pct"] = defillama.change_pct(tvl.data, "tvl", 7)
            out["tvl_30d_pct"] = defillama.change_pct(tvl.data, "tvl", 30)
        fees = defillama.fetch_chain_fees(slug)
    elif kind == "protocol" and slug:
        tvl = defillama.fetch_protocol_tvl(slug)
        prov.append(tvl)
        if isinstance(tvl.data, dict):
            out["tvl_usd"] = tvl.data.get("value")
        fees = defillama.fetch_protocol_fees(slug)
    else:
        return out, prov
    prov.append(fees)
    if isinstance(fees.data, dict):
        out["fees_24h_usd"] = fees.data.get("total24h")
        out["fees_1d_pct"] = fees.data.get("change_1d")
        out["fees_7d_pct"] = fees.data.get("change_7dover7d")
    share = tokenomics.get(asset.symbol, {}).get("token_revenue_share")
    if share is not None:
        out["token_revenue_share"] = share
    numeric = {k: n for k, v in out.items() if (n := finite_number(v)) is not None}
    return numeric, prov


def unlock_for(asset: Asset, now: datetime) -> tuple[dict[str, Any], list[Observation]]:
    """Next token unlock from the free DefiLlama dataset CDN (F-048)."""
    slug = asset.unlocks_slug
    if not slug:
        catalogue = fetch_catalogue_slugs()
        slug = unlocks.match_slug(asset.coingecko or asset.symbol, catalogue) if catalogue else None
    if not slug:
        return {}, []
    obs = unlocks.fetch_schedule(slug)
    event = unlocks.next_unlock(obs, now)
    if event is None:
        return {}, [obs]
    return (
        {
            "unlock_in_days": float(event["in_days"]),
            "unlock_pct_of_unlocked": event["pct_of_unlocked"],
            "unlock_recipients": ", ".join(event["recipients"][:3]),
            "unlock_max_30d_in_days": event["max_30d_in_days"],
            "unlock_max_30d_pct": event["max_30d_pct"],
            "unlock_pct_next_7d": event["pct_next_7d"],
            "unlock_pct_next_30d": event["pct_next_30d"],
        },
        [obs],
    )


def fetch_catalogue_slugs() -> list[str]:
    obs = unlocks.fetch_catalogue()
    return [str(s) for s in obs.data] if isinstance(obs.data, list) else []


NAME_OVERRIDES = {"BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple", "BNB": "binance coin"}


def news(asset: Asset) -> tuple[list[dict[str, Any]], list[Observation]]:
    """Primary-outlet items that mention the asset, plus Google News for breadth."""
    name = NAME_OVERRIDES.get(asset.symbol, asset.coingecko or asset.symbol)
    feeds: dict[str, Any] = {
        outlet: partial(news_rss.fetch_outlet, outlet) for outlet in news_rss.OUTLETS
    }
    feeds["google"] = lambda: news_rss.fetch_google_news(f"{name.replace('-', ' ')} crypto", days=2)
    fetched = run_parallel(feeds, workers=5)

    items: list[dict[str, Any]] = []
    prov: list[Observation] = []
    for key, result in fetched.items():
        if not isinstance(result, Observation):
            continue
        prov.append(result)
        for item in news_rss.parse_items(result):
            if key == "google":
                items.append(item | {"relevant": mentions(item["title"], asset.symbol, name)})
            elif mentions(item["title"], asset.symbol, name):
                items.append(item | {"publisher": outlet_names.get(key, key), "relevant": True})
    return items, prov


outlet_names = {
    "coindesk": "CoinDesk",
    "theblock": "The Block",
    "cointelegraph": "Cointelegraph",
    "decrypt": "Decrypt",
}


def equities(symbol: str, today: date) -> tuple[list[dict[str, Any]], list[Observation]]:
    """Crypto-treasury 8-Ks from the last 7 days — for BTC and ETH only."""
    if symbol not in {"BTC", "ETH"}:
        return [], []
    query = "bitcoin" if symbol == "BTC" else "ether"
    obs = sec_edgar.full_text_search(query, "8-K", today - timedelta(days=7), today)
    watch = sec_edgar.WATCHLIST_CIK.values()
    lines = [
        {"co": h["company"][:40], "form": h["form"], "d": h["filed"]}
        for h in sec_edgar.search_hits(obs)
        if any(f"CIK {cik}" in h["company"] for cik in watch)
    ]
    return lines[:3], [obs]


def build_dossier(
    asset: Asset,
    regime: RegimeView,
    btc_daily: pd.DataFrame,
    hl: Observation,
    engine: Engine | None = None,
    now: datetime | None = None,
) -> Dossier:
    moment = now or datetime.now(UTC)
    tokenomics = load_tokenomics()
    catalysts = load_catalysts()
    families = run_parallel(
        {
            "technical": lambda: technical(asset, btc_daily),
            "derivatives": lambda: derivatives(asset, hl),
            "onchain": lambda: onchain(asset, tokenomics),
            "unlocks": lambda: unlock_for(asset, moment),
            "news": lambda: news(asset),
            "equities": lambda: equities(asset.symbol, moment.date()),
        },
        workers=6,
    )
    tech, p1 = _unpack(families["technical"], ({}, []))
    deriv, p2, funding_history = _unpack(families["derivatives"], ({}, [], []))
    chain, p3 = _unpack(families["onchain"], ({}, []))
    unlock, p_unlock = _unpack(families["unlocks"], ({}, []))
    chain.update({k: n for k, v in unlock.items() if (n := finite_number(v)) is not None})
    p3 = [*p3, *p_unlock]
    headlines, p4 = _unpack(families["news"], ([], []))
    filings, p5 = _unpack(families["equities"], ([], []))

    daily = _frame(p1[0]) if p1 else pd.DataFrame()
    theses = asset_memory.recent_theses(engine, asset.symbol) if engine is not None else []
    card = asset_memory.build_card(
        asset.symbol,
        daily,
        funding_history,
        catalysts,
        tokenomics.get(asset.symbol, {}),
        theses,
        moment.date(),
    )
    if engine is not None:
        asset_memory.save_card(engine, asset.symbol, card, moment)

    gaps: list[str] = []
    if asset.expires is not None:
        gaps.append(f"whitelist entry expires {asset.expires.isoformat()}")
    if not (asset.defillama or {}).get("slug"):
        gaps.append("onchain: no DefiLlama mapping for this asset")
    if not unlock:
        gaps.append("unlocks: no schedule mapped for this asset (set `unlocks_slug`)")
    return assemble(
        symbol=asset.symbol,
        source=asset.source,
        now=moment,
        regime=regime,
        technical=tech,
        derivatives=deriv,
        onchain=chain,
        news=headlines,
        catalysts=catalysts,
        equities=filings,
        memory=card,
        provenance=[*p1, *p2, *p3, *p4, *p5],
        data_gaps=gaps,
    )


def instrument_spec(asset: Asset) -> InstrumentSpec:
    """Contract spec for sizing, from the OKX instrument list (F-027: never generic rounding)."""
    host = get_settings().okx_public_host.rstrip("/")
    obs = fetch_json(
        "okx.public.instrument",
        f"{host}/api/v5/public/instruments",
        1,
        "reference",
        params={"instType": "SWAP", "instId": asset.okx},
    )
    # Match by instId: a response that ignored the filter must not size DOGE with BTC's row.
    row = next(
        (r for r in _rows(obs) if isinstance(r, dict) and r.get("instId") == asset.okx), None
    )
    if row is None:
        raise ValueError(f"no OKX instrument for {asset.okx}")
    if row.get("state") != "live":
        raise ValueError(f"{asset.okx} is not tradable on OKX (state={row.get('state')!r})")
    # D-5: never default a sizing field. `ctVal or 1` turned a blank DOGE field (ctVal=1000)
    # into a 1000x oversized order; an unreadable spec stops this asset instead.
    contract_value = _positive(row, "ctVal", asset.okx) * _positive(row, "ctMult", asset.okx)
    max_leverage = int(_positive(row, "lever", asset.okx))
    if max_leverage < 1:
        raise ValueError(f"{asset.okx}: instrument lever={row.get('lever')!r} is below 1")
    return InstrumentSpec(
        contract_value=contract_value,
        lot_size=_positive(row, "lotSz", asset.okx),
        min_contracts=_positive(row, "minSz", asset.okx),
        max_leverage=max_leverage,
        maint_margin_rate=Decimal("0.01"),  # tier-1 approximation until the tier table is read
    )


def _positive(row: dict[str, Any], field: str, inst_id: str) -> Decimal:
    raw = row.get(field)
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError(f"{inst_id}: instrument {field}={raw!r} is not a number") from None
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{inst_id}: instrument {field}={raw!r} must be a positive number")
    return value


def fee_rates(
    asset: Asset | None = None, allow_default: bool = True, adapter: Any = None
) -> FeeRates:
    """Account fee rates for this asset's instrument, from the account endpoint.

    The published tier-1 defaults are a guess (F-026: real rates were 6-12x the documented ones),
    so only a run that places nothing may fall back to them (review G-14). An armed run raises,
    and the manager records that asset as failed rather than gating it on invented costs.
    """
    symbol = asset.ccxt if asset is not None else "BTC/USDT:USDT"
    try:
        if adapter is None:
            from argus.exchange.okx import OKXAdapter

            adapter = OKXAdapter()
        rates = adapter.fetch_fee_rates(symbol)
        return FeeRates(maker=rates["maker"], taker=rates["taker"])
    except Exception:
        if not allow_default:
            raise
        log.warning("fees.default", reason="account fee rates unavailable", symbol=symbol)
        return FeeRates(maker=Decimal("-0.0002"), taker=Decimal("-0.0005"))


def instrument_listed(asset: Asset) -> bool:
    """Whether OKX lists the instrument as tradable (`state == "live"`), for gate 10.

    Same request as `instrument_spec`, so it is one cache entry, not a second call. G-14:
    `in_market_list` was never set, so the gate's list check could not fire.
    """
    host = get_settings().okx_public_host.rstrip("/")
    obs = fetch_json(
        "okx.public.instrument",
        f"{host}/api/v5/public/instruments",
        1,
        "reference",
        params={"instType": "SWAP", "instId": asset.okx},
    )
    rows = _rows(obs)
    return bool(rows) and isinstance(rows[0], dict) and rows[0].get("state") == "live"


@dataclass(frozen=True)
class Quote:
    """The price the gate sizes against, read at gate time — not the last closed 4H bar."""

    mark: float | None
    spread_pct: float | None
    # Whether other venues corroborate `mark` (review D-2, F-010). No default: an unread
    # cross-check is not a passed one.
    consensus_ok: bool


# The venue whose price the gate sizes on. It must sit inside the agreeing majority: three other
# venues agreeing with each other and not with OKX leave the mark itself uncorroborated.
MARK_SOURCE = "okx.public.ticker"


def _price(value: Any) -> Decimal | None:
    number = _number(value)
    return (
        Decimal(str(number))
        if number is not None and math.isfinite(number) and number > 0
        else None
    )


# Coins the satellites list per 1000 units (F-167): (Binance/Bybit pair, Hyperliquid coin, units).
# Their quotes are the price of 1000 coins; OKX quotes one. Measured 2026-11-11 for PEPE.
_PER_1000: dict[str, tuple[str, str, int]] = {"PEPE": ("1000PEPEUSDT", "kPEPE", 1000)}


def _satellite_names(asset: Asset) -> tuple[str, str, int]:
    """(Binance/Bybit pair, Hyperliquid coin, units per quote) for `asset`."""
    symbol = asset.symbol.upper()
    return _PER_1000.get(symbol, (f"{symbol}USDT", symbol, 1))


def _venue_prices(asset: Asset, fetched: dict[str, Any]) -> dict[str, Decimal]:
    """Each venue's price for `asset`, keyed by source; an unreadable venue is simply absent."""
    obs = {
        name: (value if isinstance(value, Observation) and value.quality == "ok" else None)
        for name, value in fetched.items()
    }
    raw: dict[str, Any] = {}
    if obs["okx"] is not None and (rows := _rows(obs["okx"])) and isinstance(rows[0], dict):
        raw[MARK_SOURCE] = rows[0].get("last")
    bybit = obs["bybit"].data if obs["bybit"] is not None else None
    if isinstance(bybit, dict) and isinstance(bybit.get("result"), dict):
        listed = bybit["result"].get("list") or []
        if listed and isinstance(listed[0], dict):
            raw["bybit.kz.public"] = listed[0].get("lastPrice")
    binance = obs["binance"].data if obs["binance"] is not None else None
    if isinstance(binance, dict):
        raw["binance.futures.public"] = binance.get("markPrice")
    if obs["hyperliquid"] is not None:
        context = hyperliquid.asset_context(obs["hyperliquid"], _satellite_names(asset)[1])
        raw["hyperliquid"] = context.get("markPx") if context else None
    units = _satellite_names(asset)[2]
    prices = {name: price for name, value in raw.items() if (price := _price(value)) is not None}
    return {name: price if name == MARK_SOURCE else price / units for name, price in prices.items()}


_VENUES = (MARK_SOURCE, "bybit.kz.public", "binance.futures.public", "hyperliquid")


def live_quote(asset: Asset) -> Quote:
    """Last price, bid/ask spread and cross-venue consensus, read fresh (review D-1, D-3, D-2).

    `mark` used to be the dossier's last closed 4H close — up to four hours old — and the spread a
    constant 0.02 % because nothing wrote `spread_pct`. The fetch bypasses the cache (a 15-minute
    cached ticker is the stale price this replaces). Anything missing comes back None, which gate
    2 rejects: an unknown price is not a price.

    Consensus (F-010, DATA_SOURCES.md "price consensus rule"): OKX, Bybit and Binance read fresh,
    Hyperliquid from the run's cached snapshot (one POST covers every coin; a stale mark there can
    only fall outside a majority, never make one). `consensus_ok` needs a majority of at least three
    within `consensus.DEFAULT_TOLERANCE_PCT` with the OKX mark among them. Venues quote last (OKX,
    Bybit) or mark (Binance, Hyperliquid); basis on thin satellites can cause a false reject, never
    a false approval. Fewer than three readable
    venues is no consensus — a REJECT at gate 2, armed or shadow, like every other unknown input.
    """
    host = get_settings().okx_public_host.rstrip("/")
    pair = _satellite_names(asset)[0]
    fetched = run_parallel(
        {
            "okx": lambda: fetch_json(
                "okx.public.ticker",
                f"{host}/api/v5/market/ticker",
                1,
                "price",
                params={"instId": asset.okx},
                force=True,
            ),
            "bybit": lambda: bybit_kz_public.fetch_ticker(pair, force=True),
            "binance": lambda: binance_futures_public.fetch_premium_index(pair, force=True),
            "hyperliquid": lambda: hyperliquid.fetch_meta_and_contexts(),
        },
        workers=4,
    )
    okx = fetched["okx"]
    rows = _rows(okx) if isinstance(okx, Observation) and okx.quality == "ok" else []
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    last, bid, ask = (_number(row.get(k)) for k in ("last", "bidPx", "askPx"))
    spread = None
    if bid is not None and ask is not None and 0 < bid <= ask:
        spread = (ask - bid) / ((ask + bid) / 2) * 100
    prices = _venue_prices(asset, fetched)
    agreed = consensus_price(asset.symbol, prices)
    consensus_ok = agreed.trade_critical_ok and MARK_SOURCE in agreed.agreeing
    if not consensus_ok:
        log.warning(
            "quote.no_price_consensus",
            symbol=asset.symbol,
            agreeing=agreed.agreeing,
            missing=sorted(set(_VENUES) - set(prices)),
            outliers={name: str(price) for name, price in agreed.outliers.items()},
            detail=agreed.event.detail if agreed.event else "OKX mark outside the majority",
        )
    return Quote(
        mark=last if last is not None and last > 0 else None,
        spread_pct=spread,
        consensus_ok=consensus_ok,
    )


def market_context(
    asset: Asset,
    dossier: Dossier,
    spec: InstrumentSpec,
    fees: FeeRates,
    quote: Quote,
    listed: bool = False,
) -> MarketContext:
    """Everything the Risk Gate is told about one asset, from its dossier and a live quote.

    No `or 0` / `or 0.02` (review G-N4, G-14): an absent input stays None and gate 2 rejects it.
    A default is a guess the gate cannot tell from a measurement.
    """
    return MarketContext(
        mark=quote.mark,
        atr_pct=_number(dossier.technical.get("atr_4h_pct")),
        spread_pct=quote.spread_pct,
        funding_8h=_number(dossier.derivatives.get("funding_8h")),
        funding_interval_hours=_number(dossier.derivatives.get("funding_interval_h")),
        swing_high_20=_number(dossier.technical.get("high_20_4h")),
        swing_low_20=_number(dossier.technical.get("low_20_4h")),
        regime=dossier.regime,
        spec=spec,
        fees=fees,
        consensus_ok=quote.consensus_ok,
        derivatives_age_min=family_age_minutes(dossier, "okx.public.funding"),
        news_present=bool(dossier.news),
        next_event_hours=(
            dossier.regime.inputs.next_event_hours if dossier.regime.inputs else None
        ),
        beta_btc=_number(dossier.technical.get("beta_btc_30d")),
        # Defaults to False: an instrument nobody checked is not known to be tradable.
        in_market_list=listed,
        # L-5 / D-N3: headlines scrub() flagged as injection reach gate 2, which rejects in code.
        suspect_sources=dossier.suspect_sources,
        # Confirmed, dated catalysts only: the corroboration an `event_thesis` needs (G-N6).
        catalyst_dates=tuple(
            c["date"]
            for c in dossier.catalysts
            if isinstance(c.get("date"), date) and not c.get("unconfirmed", True)
        ),
    )


def family_age_minutes(dossier: Dossier, source: str) -> float | None:
    for obs in dossier.provenance:
        if obs.source == source and obs.asof is not None:
            return max(0.0, (dossier.as_of - obs.asof).total_seconds() / 60)
    return None
