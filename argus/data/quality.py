"""Data-quality traps 1–18 as code (DATA_SOURCES.md §9; FAILURE_LOG F-009…F-012, F-022).

Each trap was observed in the September research runs against free sources. Each gets one
function here, one test in `tests/data_quality/`, and one `data_quality_events` row when it
fires in production. The rule behind all of them: **"no data" is never zero**, and a number
without provenance never reaches a trade.

Checks raise `DataQualityEvent`; `collect()` runs several and returns what fired.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Severity = Literal["info", "suspect", "reject"]


@dataclass
class DataQualityEvent(Exception):  # noqa: N818 - it is an event first, an exception second
    """One trap firing. Carries everything the journal row needs."""

    trap: str
    source: str
    severity: Severity
    detail: str
    symbol: str | None = None

    def __str__(self) -> str:
        return f"[{self.trap}] {self.source}: {self.detail}"


def collect(*checks: Callable[[], None]) -> list[DataQualityEvent]:
    """Run checks, returning the events that fired instead of stopping at the first."""
    events: list[DataQualityEvent] = []
    for check in checks:
        try:
            check()
        except DataQualityEvent as event:
            events.append(event)
    return events


# --- trap 1 -------------------------------------------------------------
def check_price_source_precedence(source: str, symbol: str | None = None) -> None:
    """CoinGecko's homepage % column contradicts its own coin pages — use coin pages/venue API."""
    if source in {"coingecko.homepage", "cmc.listings_table"}:
        raise DataQualityEvent(
            trap="trap_01_table_vs_page",
            source=source,
            severity="reject",
            detail=(
                "aggregator summary tables are not a price source; use the coin page or venue API"
            ),
            symbol=symbol,
        )


# --- trap 2 -------------------------------------------------------------
def check_price_within_24h_range(
    price: Decimal, low_24h: Decimal, high_24h: Decimal, source: str, symbol: str | None = None
) -> None:
    """A spot price outside its own 24h range means one of the two numbers is stale."""
    if high_24h < low_24h:
        raise DataQualityEvent(
            trap="trap_02_range_sanity",
            source=source,
            severity="reject",
            detail=f"24h high {high_24h} below low {low_24h}",
            symbol=symbol,
        )
    if not low_24h <= price <= high_24h:
        raise DataQualityEvent(
            trap="trap_02_range_sanity",
            source=source,
            severity="suspect",
            detail=f"price {price} outside 24h range [{low_24h}, {high_24h}] — one value is stale",
            symbol=symbol,
        )


# --- traps 3 & 18 -------------------------------------------------------
def check_blank_not_zero(raw: Any, source: str, field: str, symbol: str | None = None) -> None:
    """Farside blank cells render as 0.0 — F-009 read −$283M of outflows as zero."""
    if raw is None or (isinstance(raw, str) and raw.strip() in {"", "-", "—", "n/a", "N/A"}):
        raise DataQualityEvent(
            trap="trap_03_blank_not_zero",
            source=source,
            severity="reject",
            detail=f"{field} is blank — record as missing, never as 0",
            symbol=symbol,
        )


def check_total_row_not_zero(rows: Sequence[Any], total: Any, source: str) -> None:
    """A 'Total 0.0' under an all-blank table is a render default, not a measurement."""
    blanks = all(r is None or (isinstance(r, str) and not r.strip()) for r in rows)
    if blanks and total is not None and Decimal(str(total)) == 0:
        raise DataQualityEvent(
            trap="trap_18_total_render_default",
            source=source,
            severity="reject",
            detail="every row blank but total renders 0.0 — the table has no data",
        )


# --- trap 4 -------------------------------------------------------------
def check_sign_preserved(text: str, value: Decimal, source: str, symbol: str | None = None) -> None:
    """CMC strips +/− in some tables, turning a fall into a rise."""
    if "%" in text and not re.search(r"[+\-−]", text) and value != 0:
        raise DataQualityEvent(
            trap="trap_04_sign_stripped",
            source=source,
            severity="suspect",
            detail=f"percentage {text!r} carries no sign — direction cannot be trusted",
            symbol=symbol,
        )


# --- trap 5 -------------------------------------------------------------
SUBSCRIPT_DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def parse_subscript_price(text: str) -> Decimal:
    """'$0.0₅3632' is 0.000003632 — the subscript counts zeros after the decimal point.

    Flattening it to 0.053632 overstates the price by four orders of magnitude.
    """
    cleaned = text.strip().lstrip("$").replace(",", "")
    match = re.match(r"^0\.0([₀-₉]+)(\d+)$", cleaned)
    if match:
        zeros = int(match.group(1).translate(SUBSCRIPT_DIGITS))
        return Decimal(f"0.{'0' * zeros}{match.group(2)}")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable price text {text!r}") from exc
    if not value.is_finite():
        raise ValueError(f"unparseable price text {text!r}")
    return value


def check_subscript_zero(
    text: str, parsed: Decimal, source: str, symbol: str | None = None
) -> None:
    try:
        expected = parse_subscript_price(text)
    except ValueError as exc:
        raise DataQualityEvent(
            trap="trap_05_subscript_zero",
            source=source,
            severity="reject",
            detail=str(exc),
            symbol=symbol,
        ) from exc
    if any(ch in text for ch in "₀₁₂₃₄₅₆₇₈₉") and parsed != expected:
        raise DataQualityEvent(
            trap="trap_05_subscript_zero",
            source=source,
            severity="reject",
            detail=f"{text!r} parsed as {parsed}, expected {expected}",
            symbol=symbol,
        )


# --- trap 6 -------------------------------------------------------------
def check_volume_plausible(
    volume_usd: Decimal, market_cap_usd: Decimal | None, source: str, symbol: str | None = None
) -> None:
    """ARB showing '$342B' of volume — a nonsense field discredits the whole row."""
    if volume_usd < 0:
        raise DataQualityEvent(
            trap="trap_06_nonsense_volume",
            source=source,
            severity="reject",
            detail=f"negative volume {volume_usd}",
            symbol=symbol,
        )
    if market_cap_usd and market_cap_usd > 0 and volume_usd > market_cap_usd * 50:
        raise DataQualityEvent(
            trap="trap_06_nonsense_volume",
            source=source,
            severity="reject",
            detail=f"volume {volume_usd} is >50x market cap {market_cap_usd} — discard the row",
            symbol=symbol,
        )


# --- trap 7 -------------------------------------------------------------
def check_timestamped_api_not_web(
    source: str, has_timestamp: bool, symbol: str | None = None
) -> None:
    """Coinalyze web columns carry no timestamp and go stale; the API returns `t`."""
    if not has_timestamp:
        raise DataQualityEvent(
            trap="trap_07_untimestamped_web",
            source=source,
            severity="reject",
            detail="value has no timestamp — use the API series and deltas, not a web column",
            symbol=symbol,
        )


# --- trap 8 -------------------------------------------------------------
def check_liquidation_source(source: str, per_asset: bool, symbol: str | None = None) -> None:
    """Aggregate liquidation totals contradict across outlets; per-asset Coinalyze is preferred."""
    if not per_asset:
        raise DataQualityEvent(
            trap="trap_08_liquidation_aggregate",
            source=source,
            severity="info",
            detail="aggregate liquidation total is soft evidence; prefer per-asset series",
            symbol=symbol,
        )


# --- trap 9 -------------------------------------------------------------
def check_single_vendor_series(
    vendors: Iterable[str], series: str, symbol: str | None = None
) -> None:
    """Mixing vendors in one OI series manufactured a false 'OI collapse' (F-022)."""
    unique = sorted(set(vendors))
    if len(unique) > 1:
        raise DataQualityEvent(
            trap="trap_09_vendor_mixing",
            source=",".join(unique),
            severity="reject",
            detail=(
                f"{series} mixes vendors {unique} — levels differ by coverage; "
                "use one vendor and Δ% only"
            ),
            symbol=symbol,
        )


# --- trap 10 ------------------------------------------------------------
# Clock skew tolerated before a publication date counts as "in the future".
FUTURE_SKEW = timedelta(minutes=5)


def check_article_recency(
    published_at: datetime | None,
    source: str,
    max_age: timedelta = timedelta(days=7),
    now: datetime | None = None,
) -> None:
    """A third of on-chain search hits were 6–12 months old presented as current (F-012)."""
    if published_at is None:
        raise DataQualityEvent(
            trap="trap_10_stale_article",
            source=source,
            severity="reject",
            detail="article has no publication date — cannot support a 'current' claim",
        )
    age = (now or datetime.now(UTC)) - published_at
    if age < -FUTURE_SKEW:
        # A future date is a mislabelled zone or a planted pubDate; either way it cannot
        # date the claim (D-18e: these used to pass as current).
        raise DataQualityEvent(
            trap="trap_10_stale_article",
            source=source,
            severity="reject",
            detail=f"article is dated {-age} in the future — its date cannot be trusted",
        )
    if age > max_age:
        raise DataQualityEvent(
            trap="trap_10_stale_article",
            source=source,
            severity="reject",
            detail=f"article is {age.days}d old — too old to evidence a current claim",
        )


# --- trap 11 ------------------------------------------------------------
def check_symbol_mapping(symbol: str, known: dict[str, str], source: str) -> None:
    """Tickers get renamed (TON → GRAM); a stale mapping silently fetches the wrong asset."""
    if symbol not in known:
        raise DataQualityEvent(
            trap="trap_11_symbol_drift",
            source=source,
            severity="reject",
            detail=f"{symbol} has no verified mapping — re-verify slugs before use",
            symbol=symbol,
        )


# --- trap 12 ------------------------------------------------------------
def check_catalyst_primary_source(url: str | None, source: str, symbol: str | None = None) -> None:
    """Aggregator calendars get dates wrong; a catalyst needs a primary source URL."""
    aggregators = ("coinmarketcal", "coingecko.com/en/events", "cryptocalendar")
    if not url or any(a in url for a in aggregators):
        raise DataQualityEvent(
            trap="trap_12_catalyst_date",
            source=source,
            severity="reject",
            detail="catalyst date must come from a primary source, not an aggregator calendar",
            symbol=symbol,
        )


# --- trap 13 ------------------------------------------------------------
def check_notional_not_loss(field: str, headline: str, source: str) -> None:
    """'$1.2B liquidated' is notional, not anyone's loss — never size on it."""
    if field in {"loss", "pnl"} and re.search(r"liquidat|notional|volume", headline, re.I):
        raise DataQualityEvent(
            trap="trap_13_notional_vs_loss",
            source=source,
            severity="suspect",
            detail=f"{field!r} taken from a notional headline: {headline[:80]!r}",
        )


# --- trap 14 ------------------------------------------------------------
def check_wallet_attribution(
    attributed_to: str | None, evidence_urls: Sequence[str], source: str
) -> None:
    """'Whale X is Alameda' is a claim until two independent sources say so."""
    if attributed_to and len(evidence_urls) < 2:
        raise DataQualityEvent(
            trap="trap_14_wallet_attribution",
            source=source,
            severity="suspect",
            detail=f"attribution to {attributed_to!r} rests on {len(evidence_urls)} source(s)",
        )


# --- trap 15 ------------------------------------------------------------
def check_cross_source_price(
    page_price: Decimal,
    venue_price: Decimal,
    source: str,
    tolerance_pct: Decimal = Decimal("1"),
    symbol: str | None = None,
) -> None:
    """A fresh timestamp is not freshness.

    loris.tools showed a current stamp with values 6% off (F-011).
    """
    if venue_price <= 0:
        raise DataQualityEvent(
            trap="trap_15_fresh_stamp_bad_values",
            source=source,
            severity="reject",
            detail="no venue price to cross-check against",
            symbol=symbol,
        )
    drift = abs(page_price - venue_price) / venue_price * Decimal("100")
    if drift > tolerance_pct:
        raise DataQualityEvent(
            trap="trap_15_fresh_stamp_bad_values",
            source=source,
            severity="reject",
            detail=(
                f"page price {page_price} is {drift:.2f}% off venue {venue_price} — "
                "distrust the whole page"
            ),
            symbol=symbol,
        )


# --- trap 16 ------------------------------------------------------------
def check_two_sources_for_print(sources: Sequence[str], figure: str) -> None:
    """Consensus figures get misreported; two sources before framing a macro print."""
    if len(set(sources)) < 2:
        raise DataQualityEvent(
            trap="trap_16_single_source_print",
            source=",".join(sources) or "none",
            severity="suspect",
            detail=f"{figure} framed from a single source",
        )


# --- trap 17 ------------------------------------------------------------
def check_price_consensus(
    prices: dict[str, Decimal], symbol: str, tolerance_pct: Decimal = Decimal("1")
) -> None:
    """Aggregators disagreed on BTC's price *and direction* by ~2% (F-010)."""
    if len(prices) < 3:
        raise DataQualityEvent(
            trap="trap_17_price_consensus",
            source=",".join(sorted(prices)) or "none",
            severity="reject",
            detail=f"price consensus needs >=3 sources, got {len(prices)}",
            symbol=symbol,
        )
    values = sorted(prices.values())
    median = values[len(values) // 2]
    outliers = {
        name: price
        for name, price in prices.items()
        if abs(price - median) / median * Decimal("100") > tolerance_pct
    }
    if len(prices) - len(outliers) < 3:
        raise DataQualityEvent(
            trap="trap_17_price_consensus",
            source=",".join(sorted(outliers)),
            severity="reject",
            detail=(
                f"only {len(prices) - len(outliers)} sources agree "
                f"within {tolerance_pct}% of {median}"
            ),
            symbol=symbol,
        )
