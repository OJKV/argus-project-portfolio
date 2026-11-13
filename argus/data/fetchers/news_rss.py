"""Free news via RSS: Google News per query + primary crypto outlets (DATA_SOURCES.md §5).

This is the zero-cost news path — and the whole news family if CryptoPanic is paid-only
(FAILURE_LOG F-044). Every item is scrubbed (§2.2) and dated; an undated or >7-day-old item
never evidences a "current" claim (trap 10).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from argus.data.envelope import Observation, Tier
from argus.data.http import fetch_json
from argus.data.quality import DataQualityEvent, check_article_recency
from argus.data.scrub import scrub

SOURCE = "news"
TIER: Tier = 1
GOOGLE_NEWS = "https://news.google.com/rss/search"
OUTLETS = {
    "theblock": "https://www.theblock.co/rss.xml",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
}
MAX_AGE = timedelta(days=7)


def fetch_google_news(query: str, days: int = 2) -> Observation:
    return fetch_json(
        f"{SOURCE}.google.{query}",
        GOOGLE_NEWS,
        TIER,
        "news",
        params={"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
        as_text=True,
        validate=feed_problem,
    )


def fetch_outlet(name: str) -> Observation:
    return fetch_json(
        f"{SOURCE}.{name}", OUTLETS[name], TIER, "news", as_text=True, validate=feed_problem
    )


def _has_dtd(text: str) -> bool:
    return "<!DOCTYPE" in text[:2048].upper() or "<!ENTITY" in text.upper()


def feed_problem(body: Any) -> str | None:
    """Why a 200 is not a feed (block page, captcha, JSON error), or None when it is one.

    Parsed as RSS such a body yields no items, which reads exactly like a quiet news day
    (D-18e); `fetch_json` returns it as `suspect`, so it lands in `data_gaps`, and does not
    cache it, so the outlet is read again once the block lifts.
    """
    text = body if isinstance(body, str) else ""
    problem: str | None = None
    if not text.strip():
        problem = "empty body - not an RSS feed"
    elif _has_dtd(text):
        problem = "body carries a DTD - not an RSS feed (block page?)"
    else:
        try:
            tag = ET.fromstring(text).tag.rsplit("}", 1)[-1].lower()
        except ET.ParseError:
            tag = None
        if tag not in {"rss", "feed", "rdf"}:
            problem = f"root <{tag or 'unparseable'}> - not an RSS feed (block page?)"
    return problem


def _published(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_items(obs: Observation, now: datetime | None = None) -> list[dict[str, Any]]:
    """RSS items as dicts. Stale or undated items are kept but marked `current=False`."""
    if not isinstance(obs.data, dict) or not obs.data.get("text"):
        return []
    text = str(obs.data["text"])
    if _has_dtd(text):
        return []  # RSS never needs a DTD; refusing one closes the entity-expansion door
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    moment = now or datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = scrub(item.findtext("title"))
        published = _published(item.findtext("pubDate"))
        source_el = item.find("source")
        try:
            check_article_recency(published, obs.source, MAX_AGE, moment)
            current, reason = True, None
        except DataQualityEvent as event:
            current, reason = False, event.detail
        items.append(
            {
                "title": title.text,
                "injection_suspect": title.injection_suspect,
                "link": (item.findtext("link") or "").strip(),
                "published": published,
                "publisher": scrub(
                    source_el.text if source_el is not None else obs.source, limit=60
                ).text,
                "current": current,
                "stale_reason": reason,
            }
        )
    return items
