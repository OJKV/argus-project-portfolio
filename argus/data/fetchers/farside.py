"""Farside ETF flows via the Jina Reader (DATA_SOURCES.md §4). Tier 1, no key.

farside.co.uk answers 403 to plain fetchers (Cloudflare), so the page is read through
`r.jina.ai`, which the design already lists for article text.

The trap this source is famous for (FAILURE_LOG F-009): a blank cell renders as "-" and a
row of blanks still prints "Total 0.0". Blank is **missing**, never zero, and a zero total
under an all-blank row means the table has no data for that day.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json

SOURCE = "farside"
# Tier 2, not 1: every byte arrives through a third-party reader (r.jina.ai), not from Farside
# itself, so a reader-side rendering change reaches us unannounced (D-12).
TIER: Tier = 2
READER = "https://r.jina.ai"
PAGES = {
    "btc": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
    "eth": "https://farside.co.uk/ethereum-etf-flow-all-data/",
}
BLANK = {"-", "–", "—", ""}
_DATE = re.compile(r"^(\d{1,2} [A-Za-z]{3} \d{4})\s*$")
_HEADER = re.compile(r"^(Date|Total)\s*$")
_CELL = re.compile(r"^\(?-?[\d,]*\.?\d+\)?$")


def fetch_btc_flows() -> Observation:
    return _fetch("btc")


def fetch_eth_flows() -> Observation:
    return _fetch("eth")


def _fetch(asset: str) -> Observation:
    return fetch_json(f"{SOURCE}.{asset}", f"{READER}/{PAGES[asset]}", TIER, "flows", as_text=True)


def _value(token: str) -> float | None:
    """ "-" is missing; "(95.1)" is an outflow; "0.0" is a real zero."""
    text = token.strip().replace(",", "")
    if text in BLANK:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def parse_flows(obs: Observation) -> list[dict[str, Any]]:
    """Rows of {date, issuers{name: flow|None}, total, suspect}, oldest first.

    The reader returns the table as tab-separated tokens spread across lines, so the text is
    flattened to a token stream: "Date" starts the header, uppercase tickers are the columns,
    and each date token opens a row of exactly len(columns) values.
    """
    if not isinstance(obs.data, dict) or not obs.data.get("text"):
        return []
    tokens = [
        t.strip().strip("|").strip()
        for line in str(obs.data["text"]).splitlines()
        for t in line.split("\t")
    ]
    tokens = [t for t in tokens if t]
    try:
        cursor = tokens.index("Date") + 1
    except ValueError:
        return []

    columns: list[str] = []
    while cursor < len(tokens) and not _DATE.match(tokens[cursor]):
        if re.fullmatch(r"[A-Z0-9]{2,6}|Total", tokens[cursor]):
            columns.append(tokens[cursor])
        cursor += 1
    # The last column must be the Total we read as the day's net flow; anything else means
    # the layout changed and values[-1] would be some issuer's number (D-12).
    if len(columns) < 2 or columns[-1] != "Total":
        return []

    rows: list[dict[str, Any]] = []
    while cursor < len(tokens):
        match = _DATE.match(tokens[cursor])
        if not match:
            cursor += 1
            continue
        when = datetime.strptime(match.group(1), "%d %b %Y").date()
        # A row's cells are the value-like tokens up to the next non-value token (the next
        # date, or the summary rows under the table). Slicing a fixed width instead let one
        # missing cell shift every later value a column left and eat the next date (D-12).
        cells: list[str] = []
        cursor += 1
        while cursor < len(tokens) and (_CELL.match(tokens[cursor]) or tokens[cursor] in BLANK):
            cells.append(tokens[cursor])
            cursor += 1
        if len(cells) != len(columns):
            rows.append({"date": when, "issuers": {}, "total": None, "suspect": True})
            continue
        values = [_value(t) for t in cells]
        issuers = dict(zip(columns[:-1], values[:-1], strict=True))
        reported = [v for v in issuers.values() if v is not None]
        total = values[-1]
        # Trap 18: "Total 0.0" under an all-blank row is a rendering default, not a zero.
        suspect = not reported or total is None or not _adds_up(reported, total)
        rows.append(
            {
                "date": when,
                "issuers": issuers,
                "total": None if suspect else total,
                "suspect": suspect,
            }
        )
    return rows


def _adds_up(reported: list[float], total: float) -> bool:
    """Issuer cells must sum to the Total within rounding (each cell is shown to 0.1)."""
    return abs(sum(reported) - total) <= 0.05 * (len(reported) + 1) + 1e-9


def net_flow(
    rows: list[dict[str, Any]],
    days: int,
    today: date,
    max_age_days: int | None = None,
) -> tuple[float | None, int]:
    """Net flow over the last `days` reporting days, and how many actually reported.

    Reporting days are bounded by a calendar window (weekends and holidays do not report), so a
    truncated or stale table cannot quietly contribute months-old rows to a "5 day" figure.
    """
    horizon = timedelta(days=max_age_days if max_age_days is not None else days * 2 + 3)
    usable = [r for r in rows if r["total"] is not None and today - horizon <= r["date"] <= today]
    if not usable:
        return None, 0
    window = sorted(usable, key=lambda r: r["date"])[-days:]
    return sum(float(r["total"]) for r in window), len(window)
