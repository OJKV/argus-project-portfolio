"""SEC EDGAR full-text search and submissions (DATA_SOURCES.md §6). Tier 1, no key.

The SEC requires a `User-Agent` naming a person and an email, and throttles at 10 req/s; without
`EDGAR_USER_AGENT` this module reports `missing` rather than calling anonymously.

The design prefers `sec-edgar-mcp` as the primary client with this raw fetcher as its fallback.
The raw path is built first because it is what the fallback must be; the MCP client is wired in
with the Manager run (Prompt 7), where the Pydantic AI MCP toolset already lives.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from argus.config import get_settings
from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json
from argus.data.scrub import scrub

SOURCE = "sec_edgar"
TIER: Tier = 1
EFTS = "https://efts.sec.gov/LATEST/search-index"
SUBMISSIONS = "https://data.sec.gov/submissions"

# Crypto-treasury and crypto-exposed equities (DATA_SOURCES.md §6); CIKs are zero-padded to 10.
# Not yet verified against sec.gov (it refuses requests without a contact User-Agent), so
# `fetch_submissions` checks the returned `tickers` and flags a mismatch (trap 11).
WATCHLIST_CIK = {
    "MSTR": "0001050446",
    "COIN": "0001679788",
    "MARA": "0001507605",
    "RIOT": "0001167419",
    "HOOD": "0001783879",
}


def _headers() -> dict[str, str] | None:
    agent = get_settings().edgar_user_agent.strip()
    if not agent or "@" not in agent:
        return None
    return {"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}


def full_text_search(
    query: str, forms: str = "8-K", start: date | None = None, end: date | None = None
) -> Observation:
    headers = _headers()
    if headers is None:
        return Observation.missing(
            f"{SOURCE}.search", TIER, "EDGAR_USER_AGENT not set (needs 'Name email@domain')"
        )
    params: dict[str, Any] = {"q": f'"{query}"', "forms": forms}
    if start and end:
        params.update(
            {"dateRange": "custom", "startdt": start.isoformat(), "enddt": end.isoformat()}
        )
    return fetch_json(f"{SOURCE}.search", EFTS, TIER, "filings", params=params, headers=headers)


def fetch_submissions(ticker: str) -> Observation:
    headers = _headers()
    if headers is None:
        return Observation.missing(
            f"{SOURCE}.submissions", TIER, "EDGAR_USER_AGENT not set (needs 'Name email@domain')"
        )
    cik = WATCHLIST_CIK[ticker.upper()]
    obs = fetch_json(
        f"{SOURCE}.submissions.{ticker.upper()}",
        f"{SUBMISSIONS}/CIK{cik}.json",
        TIER,
        "filings",
        headers=headers,
    )
    return verify_ticker(obs, ticker)


def verify_ticker(obs: Observation, ticker: str) -> Observation:
    """A CIK that resolves to a different company is a mapping error, not evidence (trap 11)."""
    if obs.quality != "ok" or not isinstance(obs.data, dict):
        return obs
    tickers = {str(t).upper() for t in obs.data.get("tickers") or []}
    if ticker.upper() in tickers:
        return obs
    return obs.model_copy(
        update={
            "quality": "suspect",
            "notes": [*obs.notes, f"CIK maps to {sorted(tickers) or 'no tickers'}, not {ticker}"],
        }
    )


def search_hits(obs: Observation) -> list[dict[str, Any]]:
    """Filing hits: form, filing date, company, and the accession id."""
    payload = obs.data if isinstance(obs.data, dict) else {}
    hits = payload.get("hits", {}).get("hits", []) if isinstance(payload.get("hits"), dict) else []
    rows: list[dict[str, Any]] = []
    for hit in hits:
        src = hit.get("_source", {})
        names = src.get("display_names") or []
        rows.append(
            {
                "form": src.get("form") or src.get("root_form"),
                "filed": src.get("file_date"),
                "company": scrub(names[0] if names else "", limit=120).text,
                "accession": (hit.get("_id") or "").split(":")[0],
            }
        )
    return rows
