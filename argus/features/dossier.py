"""Per-symbol dossier — *the* interface between code and the LLM (ARCHITECTURE.md §5.3).

`assemble()` is pure: fetched pieces in, `Dossier` out. `render()` turns it into compact JSON
with short keys and rounded numbers (LLM_AGENTS.md §4) inside a hard token budget. Every family
that is missing, and every source that failed, is listed in `gaps` — the analyst must see what it
does not know, and the Risk Gate reads the same list.

Evidence items carry `class="factual|subjective"` (ARCHITECTURE.md §6, FS-Reasoning): subjective
items may justify at most ±1 on a factor.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from argus.data.envelope import Observation
from argus.data.scrub import scrub
from argus.features.regime import RegimeView

TOKEN_BUDGET = 1500
MAX_NEWS = 8
MAX_CATALYSTS = 5
# Cross-market context is one entry per source — CoinMarketCap, Fear & Greed, ETF flows,
# FRED, mempool, prediction markets — so three was cutting half of it. A full dossier costs
# ~295 tokens of a 1500 budget, and the trimming loop still protects the ceiling.
MAX_EQUITIES = 8
CATALYST_WINDOW_DAYS = 21
MARKET_WIDE = {"MACRO", "MARKET"}

EvidenceClass = Literal["factual", "subjective"]
_SUBJECTIVE = re.compile(
    r"\b(analysts?|predicts?|prediction|could|may|might|expects?|says|said|believes?|thinks?|"
    r"opinion|bullish|bearish|why|should|forecasts?|price target|outlook|sentiment|"
    r"analysis|chart)\b",
    re.IGNORECASE,
)


_PROMO = re.compile(
    r"\b(presales?|sponsored|press release|airdrop guide|to watch|investment radar|"
    r"best crypto|top (upcoming|\d+) crypto|next \d+x|\d+x (gains?|potential)|could \d+x|"
    r"\d+x before|buy before|don'?t miss|100x|1000x|how to buy|step-by-step guide)\b"
    r"|\|\s*buy\s*&\s*sell",  # exchange listing pages ("… | Buy & Sell & Trade")
    re.IGNORECASE,
)
# Primary crypto outlets (DATA_SOURCES.md §5) outrank aggregator and PR-wire hits.
PRIMARY_OUTLETS = (
    "coindesk",
    "the block",
    "theblock",
    "cointelegraph",
    "decrypt",
    "blockworks",
    "dl news",
    "reuters",
    "bloomberg",
    "cnbc",
    "axios",
    "wu blockchain",
    "panews",
)


def is_promotional(text: str) -> bool:
    """Presale ads, "top N to watch" lists and 100x bait: advertising, not evidence."""
    return bool(_PROMO.search(text))


def _is_primary(publisher: str) -> bool:
    name = publisher.strip().lower()
    return any(outlet in name for outlet in PRIMARY_OUTLETS)


def _strip_publisher(title: str, publisher: str) -> str:
    """Google News appends " - Publisher" to every title; it costs tokens and says nothing."""
    suffix = f" - {publisher}"
    return title[: -len(suffix)].rstrip() if publisher and title.endswith(suffix) else title


def mentions(title: str, symbol: str, name: str) -> bool:
    """Whole-word match: the ticker in capitals (UNI, not "University"), or the project name."""
    if re.search(rf"(?<![A-Za-z0-9$]){re.escape(symbol.upper())}(?![A-Za-z0-9])", title):
        return True
    words = name.replace("-", " ").strip()
    return bool(words) and bool(re.search(rf"\b{re.escape(words)}\b", title, re.IGNORECASE))


def _news_order(item: dict[str, Any]) -> tuple[bool, bool, bool, datetime]:
    """Current, then titles that name the asset, then primary outlets, then newest.

    Google News matches article bodies, so a CoinDesk piece about XRP can come back for
    "solana"; `relevant` (set by the fetch layer) keeps those behind on-topic items.
    """
    published = item.get("published")
    return (
        bool(item.get("current", True)),
        bool(item.get("relevant", True)),
        _is_primary(str(item.get("publisher") or "")),
        published if isinstance(published, datetime) else datetime.min.replace(tzinfo=UTC),
    )


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Syndicated copies share a title; keep the first (best-ranked) one."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        publisher = str(item.get("publisher") or "")
        key = re.sub(r"\W+", " ", _strip_publisher(str(item.get("title") or ""), publisher))
        key = key.strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def evidence_class(text: str) -> EvidenceClass:
    """Reports of what happened are factual; predictions, opinions and questions are not."""
    return "subjective" if "?" in text or _SUBJECTIVE.search(text) else "factual"


def _sig(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    if value == 0:
        return 0.0
    return round(value, digits - 1 - int(math.floor(math.log10(abs(value)))))


def _dp(value: float | None, places: int = 1) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, places)


def _pct_sig(value: float | None) -> float | None:
    """A rate (0.0000812) shown as a percentage with 2 significant figures (0.0081)."""
    return None if value is None else _sig(value * 100, 2)


# input key → (output key, rounder)
TECH_FIELDS: list[tuple[str, str, Any]] = [
    ("price", "px", _sig),
    ("rsi_4h", "rsi4h", _dp),
    ("adx_4h", "adx4h", _dp),
    ("atr_4h", "atr4h", _sig),
    ("atr_4h_pct", "atr4h%", _dp),
    ("bb_pos_4h", "bb4h", lambda v: _dp(v, 2)),
    ("high_20_4h", "hi20_4h", _sig),
    ("low_20_4h", "lo20_4h", _sig),
    ("high_50_4h", "hi50_4h", _sig),
    ("low_50_4h", "lo50_4h", _sig),
    ("vwap_1d", "vwap1d", _sig),
    ("rel_vol_4h", "rvol4h", lambda v: _dp(v, 2)),
    ("ret_7d", "r7d%", _dp),
    ("ret_30d", "r30d%", _dp),
    ("beta_btc_30d", "beta30d", lambda v: _dp(v, 2)),
    ("rv20_pct", "rv20pct", _dp),
]
DERIV_FIELDS: list[tuple[str, str, Any]] = [
    ("funding_8h", "fund8h%", _pct_sig),
    ("next_funding_8h", "nextfund%", _pct_sig),
    ("funding_avg_7d", "fund7d%", _pct_sig),
    ("funding_pctile_30d", "fundpct", _dp),
    ("oi_24h_pct", "oi24h%", _dp),
    ("oi_7d_pct", "oi7d%", _dp),
    ("ls_ratio", "ls", lambda v: _dp(v, 2)),
    ("taker_ratio", "taker", lambda v: _dp(v, 2)),
    ("hl_funding", "hlfund%", _pct_sig),
    ("basis_pct", "basis%", lambda v: _dp(v, 2)),
]
CHAIN_FIELDS: list[tuple[str, str, Any]] = [
    ("tvl_usd", "tvl", lambda v: _sig(v, 3)),
    ("tvl_7d_pct", "tvl7d%", _dp),
    ("tvl_30d_pct", "tvl30d%", _dp),
    ("fees_24h_usd", "fees24h", lambda v: _sig(v, 3)),
    ("fees_1d_pct", "fees1d%", _dp),
    ("fees_7d_pct", "fees7d%", _dp),
    ("token_revenue_share", "rev_to_token", lambda v: _dp(v, 2)),
    ("unlock_in_days", "unlock_in_d", lambda v: _dp(v, 0)),
    ("unlock_pct_of_unlocked", "unlock%supply", lambda v: _dp(v, 2)),
    ("unlock_max_30d_in_days", "unlock_max30d_in_d", lambda v: _dp(v, 0)),
    ("unlock_max_30d_pct", "unlock_max30d%supply", lambda v: _dp(v, 2)),
    ("unlock_pct_next_7d", "emit7d%supply", lambda v: _dp(v, 2)),
    ("unlock_pct_next_30d", "emit30d%supply", lambda v: _dp(v, 2)),
]


@dataclass
class Dossier:
    symbol: str
    source: str
    as_of: datetime
    regime: RegimeView
    technical: dict[str, Any] = field(default_factory=dict)
    derivatives: dict[str, Any] = field(default_factory=dict)
    onchain: dict[str, Any] = field(default_factory=dict)
    news: list[dict[str, Any]] = field(default_factory=list)
    catalysts: list[dict[str, Any]] = field(default_factory=list)
    equities: list[dict[str, Any]] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    provenance: list[Observation] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)

    @property
    def trade_critical_present(self) -> bool:
        return bool(self.technical) and bool(self.derivatives)

    @property
    def suspect_sources(self) -> list[str]:
        """Publishers of headlines that look like prompt injection (review L-5 / D-N3).

        `scrub()` already flagged them, but the flag only reached the model — the reader being
        attacked — as an attribute in the prompt. This is what carries it to gate 2, which
        rejects the plan in code whatever the model made of the text. Checked the same two ways
        `_evidence` renders it: the fetcher's flag, or the scrubbed title itself.
        """
        found: list[str] = []
        for item in self.news:
            publisher = str(item.get("publisher") or "unknown")
            title = scrub(_strip_publisher(str(item.get("title") or ""), publisher), limit=200)
            if item.get("injection_suspect") or title.injection_suspect:
                found.append(publisher)
        return list(dict.fromkeys(found))


def match_catalysts(
    items: list[dict[str, Any]],
    symbol: str,
    today: date,
    window_days: int = CATALYST_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Dated items for this symbol (or market-wide) inside the window, soonest first."""
    picked: list[dict[str, Any]] = []
    for item in items:
        when = item.get("date")
        if not isinstance(when, date):
            continue
        scope = str(item.get("symbol", "")).upper()
        if scope != symbol.upper() and scope not in MARKET_WIDE:
            continue
        in_days = (when - today).days
        if not 0 <= in_days <= window_days:
            continue
        picked.append(
            {
                "date": when,
                "in_days": in_days,
                "kind": item.get("kind"),
                "note": scrub(str(item.get("note") or ""), limit=120).text,
                "unconfirmed": item.get("status") != "confirmed",
                "symbol": scope,
            }
        )
    return sorted(picked, key=lambda c: c["in_days"])


def assemble(
    symbol: str,
    source: str,
    now: datetime,
    regime: RegimeView,
    technical: dict[str, Any] | None = None,
    derivatives: dict[str, Any] | None = None,
    onchain: dict[str, Any] | None = None,
    news: list[dict[str, Any]] | None = None,
    catalysts: list[dict[str, Any]] | None = None,
    equities: list[dict[str, Any]] | None = None,
    memory: dict[str, Any] | None = None,
    provenance: list[Observation] | None = None,
    data_gaps: list[str] | None = None,
) -> Dossier:
    tech = {k: v for k, v in (technical or {}).items() if v is not None}
    deriv = {k: v for k, v in (derivatives or {}).items() if v is not None}
    chain = {k: v for k, v in (onchain or {}).items() if v is not None}
    gaps = list(data_gaps or [])
    for family, values in (("technical", tech), ("derivatives", deriv), ("onchain", chain)):
        if not values:
            gaps.append(f"{family}: missing")
    for obs in provenance or []:
        if obs.quality != "ok":
            reason = obs.notes[0] if obs.notes else obs.quality
            gaps.append(f"{obs.source}: {obs.quality} — {reason}")
    if not news:
        gaps.append("news: no headlines in the last 48h")
    return Dossier(
        symbol=symbol.upper(),
        source=source,
        as_of=now,
        regime=regime,
        technical=tech,
        derivatives=deriv,
        onchain=chain,
        news=[n for n in (news or []) if not is_promotional(str(n.get("title") or ""))],
        catalysts=match_catalysts(list(catalysts or []), symbol, now.date()),
        equities=list(equities or []),
        memory=dict(memory or {}),
        provenance=list(provenance or []),
        data_gaps=list(dict.fromkeys(gaps)),
    )


def _compact(values: dict[str, Any], fields: list[tuple[str, str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, short, rounder in fields:
        if key in values:
            rounded = rounder(float(values[key]))
            if rounded is not None:
                out[short] = rounded
    return out


def _ema_block(stack: dict[str, Any] | None) -> dict[str, Any] | None:
    if not stack:
        return None
    return {
        "20": _sig(stack.get("ema20")),
        "50": _sig(stack.get("ema50")),
        "200": _sig(stack.get("ema200")),
        "slope%": _dp(stack.get("slope20"), 2),
        "order": stack.get("order"),
    }


def _age(obs: Observation, now: datetime) -> str:
    if obs.quality != "ok":
        return obs.quality
    if obs.asof is None:
        return f"T{obs.tier}"
    minutes = max(0, int((now - obs.asof).total_seconds() // 60))
    if minutes < 90:
        span = f"{minutes}m"
    elif minutes < 48 * 60:
        span = f"{minutes // 60}h"
    else:
        span = f"{minutes // 1440}d"
    return f"T{obs.tier} {span}"


def _evidence(item: dict[str, Any]) -> str:
    raw_publisher = str(item.get("publisher") or "")
    title = scrub(_strip_publisher(str(item.get("title") or ""), raw_publisher), limit=200)
    published = item.get("published")
    asof = published.strftime("%Y-%m-%dT%H:%MZ") if isinstance(published, datetime) else "undated"
    publisher = scrub(str(item.get("publisher") or "unknown"), limit=40).text.replace('"', "'")
    attrs = [f'src="{publisher}"', f'asof="{asof}"', f'class="{evidence_class(title.text)}"']
    if not item.get("current", True):
        attrs.append('stale="true"')
    if item.get("injection_suspect") or title.injection_suspect:
        attrs.append('injection_suspect="true"')
    body = title.text.replace("<", "‹").replace(">", "›")
    return f"<evidence {' '.join(attrs)}>{body}</evidence>"


# Public alias: the Catalyst Extractor renders the run's headlines the same way the dossier does,
# so a source it cites is the same string the Analyst saw.
evidence_tag = _evidence


def _payload(
    d: Dossier, news: int, catalysts: int, gaps: int, equities: int = MAX_EQUITIES
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "sym": d.symbol,
        "src": d.source,
        "asof": d.as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "regime": {
            "label": d.regime.label,
            "mult_long": _dp(d.regime.risk_mult_for(d.symbol, "long"), 2),
            "mult_short": _dp(d.regime.risk_mult_for(d.symbol, "short"), 2),
            "limit_only": d.regime.limit_only,
        },
    }
    tech = _compact(d.technical, TECH_FIELDS)
    for tf in ("1h", "4h", "1d"):
        block = _ema_block(d.technical.get(f"ema_{tf}"))
        if block:
            tech[f"ema{tf}"] = block
    if tech:
        body["tech"] = tech
    deriv = _compact(d.derivatives, DERIV_FIELDS)
    if deriv:
        body["deriv"] = deriv
    chain = _compact(d.onchain, CHAIN_FIELDS)
    if chain:
        body["chain"] = chain
    if d.news:
        ordered = _dedupe(sorted(d.news, key=_news_order, reverse=True))
        body["news"] = [_evidence(n) for n in ordered[:news]]
    if d.catalysts:
        body["cat"] = [
            {
                "d": c["date"].isoformat(),
                "in": c["in_days"],
                "kind": c["kind"],
                "note": c["note"],
                **({"unconfirmed": True} if c["unconfirmed"] else {}),
            }
            for c in d.catalysts[:catalysts]
        ]
    if d.equities and equities > 0:
        body["eq"] = d.equities[: min(equities, MAX_EQUITIES)]
    if d.memory:
        body["mem"] = d.memory
    if d.data_gaps:
        body["gaps"] = [scrub(g, limit=90).text for g in d.data_gaps[:gaps]]
    body["prov"] = _provenance(d.provenance, d.as_of)
    return body


_QUALITY_RANK = {"ok": 0, "stale": 1, "suspect": 2, "missing": 3}


def _provenance(observations: list[Observation], now: datetime) -> dict[str, str]:
    """One entry per source, showing its *worst* member.

    Several observations can share a source (rubik OI at 1H and 1D). Keying a dict by source
    kept whichever came last - usually the freshest - and hid a stale or missing sibling
    (D-18c). Worst = lowest quality, then unknown age, then oldest.
    """

    def badness(obs: Observation) -> tuple[int, int, float]:
        age = (now - obs.asof).total_seconds() if obs.asof is not None else 0.0
        return (_QUALITY_RANK[obs.quality], int(obs.asof is None), age)

    worst: dict[str, Observation] = {}
    for obs in observations:
        held = worst.get(obs.source)
        if held is None or badness(obs) > badness(held):
            worst[obs.source] = obs
    return {source: _age(obs, now) for source, obs in worst.items()}


def token_count(text: str) -> int:
    """Conservative estimate (≈3.2 bytes per token for dense JSON) — overcounts, never under.

    Deliberately not tiktoken: it downloads its encoding on first use, which would put a
    network call inside an offline test and a Manager run.
    """
    return math.ceil(len(text.encode("utf-8")) / 3.2)


def render(dossier: Dossier, budget: int = TOKEN_BUDGET) -> str:
    """Compact JSON inside the budget; trims news, then catalysts, then gaps, then
    cross-market entries if it must.

    Cross-market entries are trimmed last but they are trimmable (D-N5): with MAX_EQUITIES at
    8, one verbose source could otherwise push every prompt past the budget for good.
    """
    news, catalysts, gaps, equities = MAX_NEWS, MAX_CATALYSTS, 12, MAX_EQUITIES
    while True:
        text = json.dumps(
            _payload(dossier, news, catalysts, gaps, equities),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if token_count(text) <= budget:
            return text
        if news > 3:
            news -= 1
        elif catalysts > 2:
            catalysts -= 1
        elif gaps > 4:
            gaps -= 1
        elif equities > 0:
            equities -= 1
        else:
            return text  # irreducible core; caller can see the count
