"""Universe option D: core (always) + whitelist (expiring) + ≤3 satellites (UNIVERSE_OPTIONS.md).

The satellite screen (DECISION_LOGIC.md §3) is pure over OKX's instrument and ticker lists, so
it is tested without a network. Crypto-native means OKX `instCategory == "1"`: category 3 is
tokenized equities (177 perps on 2026-09-17), category 4 commodities — a name denylist alone
would have let most of them through (FAILURE_LOG F-038).
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import structlog

from argus.config import KNOWLEDGE_DIR, load_yaml, universe_config

Source = Literal["core", "whitelist", "satellite"]
CRYPTO_CATEGORY = "1"

log = structlog.get_logger(__name__)

STABLES = {
    "USDT",
    "USDC",
    "DAI",
    "FDUSD",
    "TUSD",
    "USDE",
    "PYUSD",
    "USDD",
    "USDP",
    "EURC",
    "EURS",
    "USD1",
    "RLUSD",
    "USDS",
    "FRAX",
    "LUSD",
    "GUSD",
    "BUSD",
}
WRAPPED = re.compile(r"^(W(BTC|ETH|BNB|SOL|TRX)|STETH|WSTETH|CBETH|RETH|CBBTC|TBTC)$")
# Leveraged-token families: BTC3L/ETH5S, the UP/DOWN pairs (issued on majors only) and
# ETHBULL/XRPBEAR. A bare `(UP|DOWN|BULL|BEAR)$` also matched real coins - JUP, SYRUP (D-18d).
_UPDOWN_BASES = "BTC|ETH|BNB|XRP|ADA|DOT|LINK|LTC|TRX|EOS|XTZ|FIL|SXP|YFI|SUSHI|AAVE|UNI|1INCH|BCH"
LEVERAGED = re.compile(
    rf"^[A-Z0-9]{{2,}}\d[LS]$|^(?:{_UPDOWN_BASES})(?:UP|DOWN)$|^[A-Z0-9]{{3,}}(?:BULL|BEAR)$"
)


@dataclass
class Asset:
    symbol: str
    okx: str
    coingecko: str
    source: Source
    defillama: dict[str, str] | None = None
    expires: date | None = None
    unlocks_slug: str | None = None

    @property
    def ccxt(self) -> str:
        """The instrument as ccxt names it, which is how the OKX adapter looks markets up.
        `okx` ("BTC-USDT-SWAP") is for OKX's public REST endpoints only (F-162)."""
        return f"{self.symbol}/USDT:USDT"


@dataclass
class ScreenRow:
    symbol: str
    okx: str
    turnover_24h: float
    ret_24h: float | None = None
    ret_7d: float | None = None
    rel_volume: float | None = None
    funding: float | None = None
    oi_24h_pct: float | None = None
    attention: float | None = None
    components: dict[str, float] = field(default_factory=dict)


def core_assets(include_blocked: bool = False) -> list[Asset]:
    """Core assets. `include_blocked` is for managing open positions, never for new entries."""
    assets = [
        Asset(
            a["symbol"],
            a["okx"],
            a.get("coingecko", ""),
            "core",
            a.get("defillama"),
            unlocks_slug=a.get("unlocks_slug"),
        )
        for a in universe_config().get("core", [])
    ]
    return assets if include_blocked else _allowed(assets)


def active_whitelist(
    entries: Iterable[dict[str, Any]], today: date, expiry_days: int
) -> tuple[list[Asset], list[str]]:
    """Entries still inside their window, and the symbols that have expired."""
    active: list[Asset] = []
    expired: list[str] = []
    for entry in entries:
        renewed = entry.get("renewed")
        if not isinstance(renewed, date):
            expired.append(str(entry.get("symbol")))
            continue
        expires = renewed + timedelta(days=expiry_days)
        if today > expires:
            expired.append(str(entry["symbol"]))
            continue
        active.append(
            Asset(
                entry["symbol"],
                entry["okx"],
                entry.get("coingecko", ""),
                "whitelist",
                entry.get("defillama"),
                expires,
                entry.get("unlocks_slug"),
            )
        )
    return active, expired


def whitelist_assets(
    today: date | None = None, include_blocked: bool = False
) -> tuple[list[Asset], list[str]]:
    entries = load_yaml("whitelist.yaml", KNOWLEDGE_DIR).get("entries") or []
    days = int(universe_config().get("whitelist_expiry_days", 30))
    active, expired = active_whitelist(entries, today or datetime.now(UTC).date(), days)
    return (active if include_blocked else _allowed(active)), expired


def blocklist() -> set[str]:
    data = load_yaml("blocklist.yaml", KNOWLEDGE_DIR)
    return {str(s).upper() for s in data.get("symbols") or []}


def _allowed(assets: list[Asset]) -> list[Asset]:
    """Drop blocklisted symbols and excluded kinds from a hand-maintained list.

    The blocklist used to guard only the satellite screen, so a blocked symbol left on the
    whitelist, or added to core, stayed tradable in every run (D-15).
    """
    blocked = blocklist()
    kept = [a for a in assets if not _refused(a.symbol, blocked)]
    dropped = sorted({a.symbol for a in assets} - {a.symbol for a in kept})
    if dropped:
        log.warning("universe.blocked", symbols=dropped)
    return kept


def refused(symbol: str) -> bool:
    """Blocklisted or an excluded kind: never a new entry, from any list or from
    `argus manager --symbols` (D-15). An open position on one is still managed."""
    return _refused(symbol, blocklist())


def _refused(symbol: str, blocked: set[str]) -> bool:
    return symbol.upper() in blocked or _excluded_kind(symbol.upper())


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _excluded_kind(symbol: str) -> bool:
    return symbol in STABLES or bool(WRAPPED.match(symbol)) or bool(LEVERAGED.search(symbol))


def screen(
    instruments: Sequence[dict[str, Any]],
    tickers: Sequence[dict[str, Any]],
    now: datetime,
    blocklist: set[str] | None = None,
    exclude: set[str] | None = None,
    already_listed: set[str] | None = None,
) -> list[ScreenRow]:
    """Satellite candidates: liquid, seasoned, crypto-native USDT perps (DECISION_LOGIC.md §3)."""
    cfg = universe_config()
    rules = cfg.get("satellites", {}).get("screen", {})
    min_turnover = float(rules.get("min_vol_24h_usd", 30e6))
    min_age = timedelta(days=int(rules.get("min_age_days", 90)))
    min_lev = float(rules.get("min_max_leverage", 10))
    max_spread = float(rules.get("max_spread_pct", 0.06))
    denylist = {str(s).upper() for s in cfg.get("non_crypto_denylist") or []}
    skip = (blocklist or set()) | (exclude or set()) | (already_listed or set()) | denylist
    by_id = {t["instId"]: t for t in tickers}

    rows: list[ScreenRow] = []
    for inst in instruments:
        inst_id = str(inst.get("instId", ""))
        if not inst_id.endswith("-USDT-SWAP") or inst.get("state", "live") != "live":
            continue
        symbol = inst_id.split("-")[0].upper()
        if symbol in skip or _excluded_kind(symbol):
            continue
        if str(inst.get("instCategory", "")) != CRYPTO_CATEGORY:
            continue
        listed = _float(inst.get("listTime"))
        if listed is None or now - datetime.fromtimestamp(listed / 1000, UTC) < min_age:
            continue
        if (_float(inst.get("lever")) or 0) < min_lev:
            continue
        tick = by_id.get(inst_id)
        if tick is None:
            continue
        last, bid, ask = (
            _float(tick.get("last")),
            _float(tick.get("bidPx")),
            _float(tick.get("askPx")),
        )
        if not last or not bid or not ask:
            continue
        turnover = (_float(tick.get("volCcy24h")) or 0) * last
        spread = (ask - bid) / ask * 100
        if turnover < min_turnover or spread > max_spread:
            continue
        open24h = _float(tick.get("open24h"))
        ret_24h = (last / open24h - 1) * 100 if open24h else None
        rows.append(ScreenRow(symbol, inst_id, turnover, ret_24h=ret_24h))
    return rows


def drop_mania(rows: list[ScreenRow]) -> tuple[list[ScreenRow], list[tuple[str, str]]]:
    """Filter out names that already made their move (UNIVERSE_OPTIONS.md; owner 2026-09-17).

    The attention score rewards extremity, so without this the screen degenerates into a
    top-gainers list. An unknown component never drops a name — only a measured breach does.
    """
    guard = universe_config().get("satellites", {}).get("mania_guard", {})
    max_ret = float(guard.get("max_abs_ret_24h_pct", 50))
    max_rvol = float(guard.get("max_rel_volume", 20))
    kept: list[ScreenRow] = []
    dropped: list[tuple[str, str]] = []
    for row in rows:
        if row.ret_24h is not None and abs(row.ret_24h) > max_ret:
            dropped.append((row.symbol, f"24h move {row.ret_24h:+.0f}% > {max_ret:.0f}%"))
        elif row.rel_volume is not None and row.rel_volume > max_rvol:
            dropped.append((row.symbol, f"volume {row.rel_volume:.0f}x > {max_rvol:.0f}x normal"))
        else:
            kept.append(row)
    return kept, dropped


def enrich(row: ScreenRow, daily: Any, funding: float | None) -> ScreenRow:
    """Fill the attention components that need per-instrument data (daily candles, funding)."""
    from argus.features import indicators

    if daily is not None and len(daily) > 7:
        row.ret_7d = indicators.period_returns(daily, (7,)).get("ret_7")
        row.rel_volume = indicators.relative_volume(daily, lookback=min(20, len(daily) - 1))
    row.funding = funding
    return row


def _zscores(values: list[float | None]) -> list[float]:
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return [0.0 for _ in values]
    mean, stdev = statistics.fmean(present), statistics.pstdev(present)
    if stdev == 0:
        return [0.0 for _ in values]
    return [0.0 if v is None else (v - mean) / stdev for v in values]


def rank_by_attention(
    rows: list[ScreenRow], catalyst_symbols: set[str] | None = None
) -> list[ScreenRow]:
    """attention = Σ z(|component|) + 2·[catalyst ≤ 14d]; missing components count as 0."""
    components = {
        "ret_24h": [abs(r.ret_24h) if r.ret_24h is not None else None for r in rows],
        "ret_7d": [abs(r.ret_7d) if r.ret_7d is not None else None for r in rows],
        "rel_volume": [r.rel_volume for r in rows],
        "oi_24h_pct": [abs(r.oi_24h_pct) if r.oi_24h_pct is not None else None for r in rows],
        "funding": [abs(r.funding) if r.funding is not None else None for r in rows],
    }
    z = {name: _zscores(values) for name, values in components.items()}
    catalysts = catalyst_symbols or set()
    for index, row in enumerate(rows):
        row.components = {name: z[name][index] for name in z}
        if row.symbol in catalysts:
            row.components["catalyst"] = 2.0
        row.attention = sum(row.components.values())
    return sorted(rows, key=lambda r: r.attention or 0.0, reverse=True)


def select(
    core: list[Asset],
    whitelist: list[Asset],
    ranked: list[ScreenRow],
    max_satellites: int,
    max_dossiers: int,
) -> list[Asset]:
    """Core always; then whitelist and satellites while the dossier budget lasts."""
    chosen = list(core)
    listed = {a.symbol for a in chosen}
    for asset in whitelist:
        if len(chosen) >= max_dossiers:
            break
        if asset.symbol not in listed:
            chosen.append(asset)
            listed.add(asset.symbol)
    satellites = 0
    for row in ranked:
        if satellites >= max_satellites or len(chosen) >= max_dossiers:
            break
        if row.symbol in listed:
            continue
        chosen.append(Asset(row.symbol, row.okx, "", "satellite"))
        listed.add(row.symbol)
        satellites += 1
    return chosen
