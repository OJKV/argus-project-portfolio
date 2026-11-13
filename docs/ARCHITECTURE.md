# ARGUS — Architecture Design Document
**Project:** ARGUS (working name) — LLM-assisted, evidence-graded swing-trading agent for crypto perpetuals
**Owner:** owner · **Version:** 1.0 (design, pre-build) · **Date:** 2026-09-14 (v0.9: 2026-09-13)
**Exchange (MVP):** OKX USDT-margined perpetual swaps · **Later:** Binance / Bybit via the same adapter interface
**Horizon:** 1–7 days · **Leverage:** 2–10x (risk-derived, not chosen by the LLM) · **Cadence:** scheduled runs, not 24/7

Companion docs: `DATA_SOURCES.md` · `DECISION_LOGIC.md` · `LLM_AGENTS.md` · `LLM_PROVIDERS_AND_MODELS.md` · `MEMORY_AND_STATE.md` · `OBSERVABILITY_AND_GUARDRAILS.md` · `UI_DESIGN.md` · `UNIVERSE_OPTIONS.md` · `STRATEGY_STACKS.md` · `ROADMAP.md` · `BUILD_PLAN.md` · `SAFETY.md` · `FAILURE_LOG.md` · `STATUS.md` · `OPEN_QUESTIONS.md` · `NAMING.md` · `REVIEW-scira-doc-and-prototype.md`

---

## 0. Design thesis (read this first)

Three facts drive every choice below:

1. **LLMs given full trading autonomy lose money.** In nof1's Alpha Arena (Oct–Nov 2025, $10k real capital each, Hyperliquid perps), four of six frontier models finished −30% to −63% in ~2 weeks; the winner traded least (~43 trades). The failure modes were sizing, exits and overtrading — not "market view".
   → **The LLM is an analyst that proposes. Code decides size, leverage, gates, execution and safety.** The LLM never sees the API keys, never sets size, cannot widen a stop, cannot override a gate.

2. **Inference and data have budget constraints.** A role-per-symbol design can multiply calls and exceed those constraints.
   → **Batch, cache, compact.** One regime call, a few analyst calls over candidate batches, one red-team call, one trader call, one reviewer call per run.

3. **Free crypto data is unreliable in specific, repeatable ways.** Known data-quality traps include: blank ETF cells rendered as 0, aggregators disagreeing on a major's *direction* by 2%, stale articles presenting as current, corrupted-but-freshly-stamped derivative pages.
   → **Every number carries provenance (source, tier, observed_at). Two-source rule for anything that drives a trade. "No data" ≠ 0. Data-quality gates can veto a trade.**

Everything else — the two-part Portfolio Manager / Portfolio Reviewer split, five-factor scoring, exchange-side stops, the watchdog, the journal — follows from these three.

---

## 1. Goals, non-goals, success criteria

**Goals**
- Take 1–7 day directional perp trades (long/short) on BTC/ETH/majors + strong alts, decided from multi-factor evidence (technical, derivatives positioning, on-chain/fundamental, macro, news/catalysts, SEC filings for crypto-treasury equities).
- Run 2 Manager sessions + 4 Review sessions per day at near-zero marginal cost.
- Be safe while asleep: protection lives on the exchange; a cheap no-LLM watchdog checks it.
- Produce a full, replayable decision journal so the strategy can be evaluated and improved.
- Grow capital only on evidence (calibration + expectancy gates), not on optimism.

**Non-goals (MVP)**
- HFT / market-making / grid / arbitrage. Sub-hour reactions.
- Multi-exchange execution (design for it, ship after MVP).
- Stocks (separate instance later; the pipeline shape transfers).
- SaaS packaging (keep the core headless and config-driven so it *can* be wrapped later).

**Success criteria (in order)**
1. Demo phase (4+ weeks): zero unprotected positions ever observed by the watchdog; ≥30 gated plans; calibration error < 15 points per score bucket.
2. Live-small phase: profit factor ≥ 1.3 over ≥30 closed trades, max DD ≤ 12% of equity, fee+funding drag < 25% of gross profit.
3. Only then scale risk/trade and capital (see `BUILD_PLAN.md` gates).

---

## 2. Reference designs and what we take from each

| Source | What we borrow | What we don't |
|---|---|---|
| **Your prototype** (`main.py`, `research_agent.py`, `portfolio_reviewer.py`, `trade_executor.py`, `market_data.py`) | Two-phase loop (review → research), ccxt+OKX swap setup, isolated margin + posSide handling, OKX algo trailing stop call, CryptoPanic/DefiLlama/CMC fetchers, Pydantic signal schema, model fallback ("smart roll") | "MUST generate ≥1 signal" prompt (forces overtrading), size = % of balance ignoring stop distance, CMC top-gainers universe (micro-cap bias), TP/SL placed *after* market entry (race window), no journal of rejected ideas, no reconciliation, no freshness stamps |
| **Neighbor chat research runs** (`trade-plans_*.md`, `SOURCES-crypto-signals.md`) | Five-factor scoring +2..−2 with "≥ +4 and no −2" rule, dated catalyst calendar, per-plan kill-switches, source tiers, 18 documented data traps, "observed vs claimed" tagging, "report what could not be verified" | Nothing — this is the method; we mechanise it |
| **TradingAgents** (Xiao et al. 2024; repo v0.4.0, Aug 2026) | Role separation and hand-offs; `deep_think` vs `quick_think` model tiers; provider-native structured output (json_schema / response_schema / tool-use); five-tier rating scale; **persistent decision log with outcome-grounded reflections** (realised return vs benchmark → one-paragraph reflection → same-asset + cross-asset lessons injected into the PM prompt); aggressive/neutral/conservative **risk debaters**; point-in-time / look-ahead fixes; Trader price grounding; checkpoint resume | One LLM per role per symbol per ticker (~10+ calls); LangGraph as the spine; equities data vendors; their Sharpe 8.21 / DD 0.91% backtest is not a live track record |
| **CryptoTrade (EMNLP 2024), FinMem** | Reflection memory: post-trade lessons fed back into prompts, with decay | Vector DB (unnecessary at our scale) |
| **Freqtrade** (you ran it for months) | Dry-run/paper mode, *protections* (StoplossGuard, MaxDrawdown, CooldownPeriod, LowProfitPairs), pairlist filters (volume/spread/age/price), ROI/time-based exits, `custom_stoploss` semantics, Telegram control, backtesting of the *rule layer* | Per-candle strategy loop; single-exchange spot bias; indicator-only decisions |
| **nautilus_trader** | Reconciliation on startup, idempotent client order IDs, event-sourced order/position journal, deterministic replay of a run from cached inputs | The always-on event-driven node, Rust build, WebSocket-first design — wrong shape for a cron bot |
| **Hyper-Alpha-Arena, kojott/LLM-trader-test, TradeMind** | Factor injection into prompts, replay harness over cached inputs, trade attribution by factor, circuit breakers, daily loss limits | UI-first scope |
| **Scira design doc v1/v2** (reviewed in `REVIEW-scira-doc-and-prototype.md`) | Agent role taxonomy, output schemas, risk-limit matrix; v2: verbal-feedback Reflect agent, inference gating (LLM only on anomalies — post-MVP wake-up), shadow-first gate with ≥50 shadow decisions, two-gate verification for code, tiered context for Claude Code, fact-vs-subjective evidence tagging, downside-miss monitor, monitoring thresholds, Kalshi/CoinStats-MCP additions | Cost model ($65–355/mo), paid on-chain APIs, LangGraph checkpointing, (Conformal) Kelly-with-LLM-confidence sizing, Shapley "Market Regime Council" (needs hundreds of trades to mean anything) |
| **Scira best-practices doc** (production failure patterns, Chis 2026) | Position **state machine** with terminal states + TTLs (246 ghost positions), atomic persistence, retries with jitter that count *attempts*, per-dependency **circuit breakers**, validate every exchange response (zero-qty fills), **verify fees from fills not docs** (real rates were 6–12× documented), per-instrument precision, pending-order TTL + price-drift cancel, absolute paths | Tick-level backtesting (irrelevant at 1–7d), WebSocket-first (post-MVP), NFT/mempool signals |

**Framework decision: in-house Python core.** Freqtrade and Nautilus both assume a continuously running engine that reacts to each candle/tick. ARGUS is a *batch decision process* that runs six times a day, talks to LLMs, and stores plans with human-readable theses and invalidation rules. Bending either framework to that costs more than writing ~3–4k lines of typed Python. We do reuse **ccxt** (exchange abstraction), **pandas-ta** (indicators), **Pydantic v2** (all contracts), **Pydantic AI** (typed agent calls + provider-native structured output + Logfire tracing), and keep the door open to Nautilus for a future "fast-exit" component. **No LiteLLM** (rejected after the March 2026 supply-chain compromise); the provider router is ~200 lines of our own code on top of Pydantic AI's model classes.

---

## 3. System overview

```
                          ┌─────────────────────────────────────────────────────────────┐
                          │  SCHEDULER (systemd timers on VPS)                           │
                          │  00:00/06:00/12:00/18:00 UTC → REVIEW   six UTC slots below → MANAGER │
                          │  every 20 min → WATCHDOG (no LLM)        Sun 09:30 → REFLECT  │
                          └───────────────┬─────────────────────────────────────────────┘
                                          ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│  RUN CONTEXT  (run_id, mode=demo|live|approve, config snapshot, kill-switch state)          │
└───────────────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ 1 DATA LAYER     │   │ 2 FEATURE LAYER  │   │ 3 ANALYSIS LAYER │   │ 4 RISK GATE (code)   │
│ fetchers+cache   │──▶│ regime, screen,  │──▶│ LLM: analysts,   │──▶│ score/RR/size/lev/   │
│ provenance,      │   │ per-symbol       │   │ red-team, trader │   │ heat/corr/events/    │
│ quality checks   │   │ dossiers (JSON)  │   │ (structured out) │   │ cooldowns/data-qual  │
└──────────────────┘   └──────────────────┘   └──────────────────┘   └──────────┬───────────┘
                                                                                 ▼
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
│ 8 MEMORY         │◀──│ 7 JOURNAL        │◀──│ 6 TELEGRAM       │◀──│ 5 EXECUTION (code)   │
│ lessons, sources,│   │ every input,     │   │ alerts, approve/ │   │ bracket orders w/    │
│ calendar, slugs  │   │ output, order    │   │ reject, control  │   │ exchange-side SL/TP, │
└──────────────────┘   └──────────────────┘   └──────────────────┘   │ reconcile            │
                                                                     └──────────────────────┘
                                          ▲
                          ┌───────────────┴───────────────┐
                          │ EXCHANGE ADAPTER (ccxt: OKX) │  ← Binance/Bybit later, same interface
                          └───────────────────────────────┘
```

Three run types share layers 1, 2, 7, 8:

| Run | Frequency | LLM calls | Purpose |
|---|---|---|---|
| **MANAGER** (Portfolio Manager) | 2×/day (00:30 UTC after daily close; 12:30 UTC pre-US) | ~6 | Regime → screen → dossiers → analysts → red-team → trader → gate → place bracket orders (or queue for approval) |
| **REVIEW** (Portfolio Reviewer) | 4×/day (on the 6h-candle close, ≥30 min before each manager run) | 1 (0 if no positions) | Sync positions, deterministic checks (SL present? TP1 hit? time stop? kill-switch?), then LLM review → HOLD/CLOSE/PARTIAL/MOVE_SL/TRAIL → gate → execute |
| **WATCHDOG** | every 20 min | 0 | Verify every position has a live exchange-side SL, margin ratio OK, portfolio DD kill-switch, data staleness, API health; repair or halt; alert |
| **REFLECT** | weekly | 1–2 | Post-mortem over closed trades → candidate lessons (with evidence counts) → prompt memory |

---

## 4. Layer 1 — Data layer

**Principle:** fetch once per run, cache with TTL, stamp provenance, validate, degrade gracefully. Every fetcher returns the same envelope:

```python
class Observation(BaseModel):
    source: str            # "okx.public.candles", "defillama.fees", "farside.btc"
    tier: Literal[1, 2, 3] # from DATA_SOURCES.md; tier 3 never drives a trade alone
    observed_at: datetime  # when WE fetched
    asof: datetime | None  # what time the data refers to (printed timestamp, candle close)
    quality: Literal["ok", "stale", "suspect", "missing"]
    notes: list[str]       # e.g. "farside blank cell → missing, not zero"
    data: dict | list
```

**Fetcher families** (full catalog, limits, endpoints and fallbacks in `DATA_SOURCES.md`):

| Family | MVP sources (free) | Cadence | Cache TTL |
|---|---|---|---|
| Prices / OHLCV | OKX public (1h/4h/1d, 300 bars), Binance futures public for deep history/backtests | every run | 15 min |
| Derivatives | OKX public: funding (current+history), OI, mark/index, liquidation orders, *rubik* long/short ratio, taker volume, OI+volume; Binance futures public: OI hist, top/global L/S, taker ratio, basis; Coinalyze (free key, 40/min): aggregated OI/funding/predicted funding/liquidations/L-S history; Hyperliquid `info` (no key): funding, OI, premium; Deribit public: DVOL, put/call | every run | 15 min |
| On-chain / fundamental | DefiLlama (TVL, fees, revenue, stablecoins, emissions/unlocks, chain flows, DEX vol), mempool.space, blockchain.info charts, Etherscan V2 free, Dune free API (saved queries for exchange netflow), Lookonchain token feeds (text scrape, tier 1 per live tests), bitcointreasuries.net | Manager runs | 4 h |
| Macro | FRED (DGS10, DFII10 real yield, DTWEXBGS DXY, FEDFUNDS), Polymarket Gamma (Fed odds, BTC price markets), Cleveland Fed nowcast (scrape), Investing.com Fed Rate Monitor (scrape), ForexFactory weekly calendar JSON (event blackout), BLS API | Manager runs | 6 h |
| ETF / flows | Farside (scrape; blank ≠ 0), WuBlockchain/PANews relays | Manager runs | 6 h |
| News / sentiment | CryptoPanic (free dev key ×2, votes as sentiment), Google News RSS per symbol, Alternative.me F&G, Reddit API (or Scira `/api/reddit`), Scira `/api/xsearch` (free 20/day → top-3 candidates only), Scira `/api/search` for open-ended "dynamic" queries | Manager runs | 2 h |
| SEC / equities | EDGAR full-text search API (`efts.sec.gov`, User-Agent required, ≤10 req/s) for 8-K/10-Q of MSTR, COIN, MARA, RIOT, CLSK, HOOD, SMLR, MTPLF; EDGAR submissions JSON | Manager runs | 6 h |
| Calendar | `knowledge/catalysts.yaml` (curated + LLM-extracted from news, human-confirmed) | every run | — |

**Cross-cutting mechanics**
- `httpx` + `tenacity` (retry w/ jitter), per-host token-bucket rate limiter, `diskcache`/SQLite cache keyed by (source, params, TTL bucket).
- **Quality checks (code, from the neighbor-chat trap list):** price sanity vs 24h range; multi-source price consensus (majority of ≥3, prefer venue pages / exchange API); percent-sign stripping; subscript-zero notation; blank-vs-zero; timestamp freshness (max age per source); OI/funding vendor mixing forbidden (Δ% only, one vendor per series); article date filter (drop > 7d old for "current" claims).
- **Degradation:** missing family → dossier field `null` + `data_gaps` entry; the Risk Gate reduces size or vetoes when *trade-critical* families are missing (see `DECISION_LOGIC.md` §6).

---

## 5. Layer 2 — Feature layer (deterministic, no LLM)

### 5.1 Regime snapshot (per Manager run)
Computed in code and placed at the top of every prompt:
- BTC: 1d/4h trend (EMA 20/50/200 stack), 20d realized vol percentile, distance to 365-DMA, daily-close levels for kill-switches.
- Breadth: % of universe above 20d EMA; BTC dominance 7d Δ; ETH/BTC 7d Δ.
- Derivatives: market-wide funding (median top-20), aggregate OI 24h/7d Δ, F&G.
- Liquidity: stablecoin supply 7d Δ (DefiLlama), ETF net flow 5d (Farside).
- Macro: 10Y real yield & Δ, DXY Δ, Fed-odds Δ, next high-impact events (≤72h).
- **Regime label** (rule table in `DECISION_LOGIC.md` §2): `TREND_UP / TREND_DOWN / CHOP / EVENT_RISK` → sets **risk multiplier** (1.0 / 1.0 / 0.5 / 0.4) and `limit_only` flag.

### 5.2 Universe & candidate screen (per Manager run)
Freqtrade-style pairlist filters, then ranking:
1. **Universe:** OKX USDT perps; 24h volume ≥ $30M; listed ≥ 90 days; max leverage ≥ 10x; spread ≤ 0.06%; exclude stablecoins, wrapped, leveraged tokens, anything on the manual blocklist.
2. **Exclude:** open positions, symbols in cooldown (recent stop-out), symbols with a pending unfilled plan.
3. **Rank** by an "attention score" (code): |24h/7d return z-score| + relative volume + |OI Δ 24h| + funding extremity + |TVL/fees 7d Δ| + news volume 24h + catalyst within 14d. Take **top 8 + BTC, ETH, SOL always**. (Not CMC "top gainers" — the neighbor runs showed that list omits genuine large-cap movers and is dominated by micro-caps.)

### 5.3 Per-symbol dossier (compact JSON, ≤ ~1.5k tokens)
Built for each candidate and each open position:
- `technical`: price, 1h/4h/1d EMA stack & slope, RSI14, ADX14, ATR14 (abs & %), Bollinger position, 20/50-bar swing highs/lows (structure S/R), VWAP(1d), rel-volume, 7d/30d return, beta to BTC (30d), distance to nearest liquidation cluster proxy.
- `derivatives`: funding now / predicted / 7d avg / percentile, OI 24h & 7d Δ%, OI/mcap, top-trader L/S, taker buy/sell ratio, 24h liquidations long/short (per-asset, Coinalyze), basis.
- `onchain_fundamental`: TVL 7d/30d Δ, fees & revenue 7d Δ, token-holder share of revenue (if known from `knowledge/tokenomics.yaml`), stablecoin flow on its chain, next unlock (date, % circ, recipient class), dev activity (GitHub commits 30d), exchange netflow (if Dune query exists for the asset).
- `news`: top 8 headlines 48h with source, published_at, CryptoPanic votes; Scira X summary + engagement (top-3 candidates only); Reddit summary.
- `catalysts`: dated items ≤ 21d from `catalysts.yaml` matched by symbol/sector.
- `macro` (shared): regime snapshot.
- `equities` (BTC/ETH only): latest MSTR/COIN 8-K summary lines if < 7d.
- `data_gaps`: list of families missing/stale/suspect with reason.
- `provenance`: for each numeric field, `[source, tier, asof]` (kept in DB; a condensed version goes to the prompt).

The dossier is *the* interface between code and LLM. It is stored in full (compressed) in the journal so any run can be replayed offline.

---

## 6. Layer 3 — Analysis layer (LLM; contracts in `LLM_AGENTS.md`)

Call budget (v1.0): Manager run ≈ 9–11, Review run ≈ 1–2, watchdog 0, weekly reflection ≈ 2 → **≈ 30–40/day**, ceiling 100/day across 8+ free providers (`LLM_PROVIDERS_AND_MODELS.md`). More agents are allowed where they *add a distinct view*; not where they restate one. Every call: JSON-schema-constrained output → Pydantic validation → one repair attempt → fallback model → skip with `data_gap` (never "make something up").

| Step | Agent | Input | Output |
|---|---|---|---|
| A | **Regime Analyst** (1 call) | regime snapshot + macro dossier | `RegimeView`: narrative ≤80 words, top 3 risks, event map, suggested posture (long-bias / short-bias / neutral / stand-aside), confidence |
| B | **Factor Analyst** (1 call per batch of 3–4 dossiers → ~3 calls) | dossiers + regime view + active lessons | per symbol: `FactorScores` (fundamental, onchain, macro, catalyst, technical ∈ {−2..+2} **with one cited evidence item per score**), `thesis_long`, `thesis_short`, `invalidation`, `data_gaps_ack` |
| C | **Red Team** (1 call for top-3 by score) | analyst output + dossiers | for each: strongest counter-case, what would have to be true, "traps in this data" (stale/aggregator conflicts), revised score deltas |
| D | **Trader / PM** (1 call) | A+B+C, open positions, portfolio state, risk budget | ≤2 `TradePlan` (entry zone, stop w/ basis, ≥2 targets w/ close %, invalidation rules, time stop, suggested leverage ≤10, confidence %) or **`NO_TRADE` with reason**. No-trade is a first-class outcome. |
| E | **Reviewer** (1 call, Review runs) | open positions + their original plans + fresh dossiers + deterministic flags | per position: `HOLD / CLOSE / PARTIAL_CLOSE(pct) / MOVE_SL(price) / ACTIVATE_TRAIL`, `thesis_status`, reason |
| C2 | **Risk Debaters** (1 call, three personas in one structured call: aggressive / neutral / conservative — TradingAgents' risk team without 3× calls) | top plans from D (draft) + portfolio state | per plan: three stances, each with a sizing-multiplier suggestion ∈ {0.5, 0.75, 1.0} and a named risk; code takes the **minimum** of the three |
| D2 | **Consensus Trader** (1 call, different provider family) | same input as D | second `TraderDecision`; direction disagreement ⇒ `NO_TRADE`; every agreement/disagreement is journaled with both rationales (`consensus_events`) so the value of the mode is measurable |
| F | **Reflector** (weekly) | closed trades (plan + outcome + review trail) | ≤10 candidate lessons with supporting trade IDs and confidence; promoted only after ≥3 supporting trades |
| F2 | **Post-mortem** (1 short call per closed trade, batched at Review) | plan, review trail, fills, outcome R, exit reason, realised vs benchmark (BTC) return | ≤120-word verbal feedback stored per trade (Scira v2 §4.4 / TradingAgents decision log); the last 5 same-asset + 5 cross-asset feedbacks are injected into Analyst/Trader prompts |
| G | **Catalyst Extractor** (1 call, Manager) | 48h headlines | dated `CatalystCandidate`s with source URL; confirmed by you in Telegram before entering `catalysts.yaml` |
| H | **Polymarket Watch** (folded into A, 0 extra calls) | Polymarket/Kalshi markets from the macro dossier | ≤3 `polymarket_watch` items (market, odds, view) for the daily summary — advisory only |

**Consensus mode is ON from day one (demo and live)** per your decision; the `consensus_events` table + `/weekly` report show how often the two families disagree, on which regimes/assets, and what the shadow ledger says the vetoed trades would have done.

**Prompt discipline (enforced by contract, checked by code):**
- Scores must cite dossier evidence by field path; a score without evidence is set to 0 by the validator.
- The model may not state a price not present in the dossier; entry/stop/targets must be within ±15% of dossier price and consistent with side.
- `data_gaps` in the dossier must be acknowledged; missing trade-critical family ⇒ max |score| = 1 for that factor.
- Prompts contain **regime, lessons, and the trap list** — not the full source list.
- Evidence items carry `evidence_class: factual | subjective` (FS-Reasoning): subjective items (X/Reddit mood, analyst opinions) can justify at most ±1 on any factor and are weighted up only in `TREND_UP` regimes; factual items (flows, filings, prints) carry full weight.

---

## 7. Layer 4 — Risk Gate (deterministic; the LLM cannot bypass it)

Runs on every `TradePlan` and every `ReviewAction`. Output: `APPROVED | ADJUSTED | REJECTED` + reasons, all journaled (rejected plans are the calibration dataset).

**Entry gates (all must pass):**
1. **Score gate:** total ≥ +4 and no factor = −2 (from the neighbor method); red-team-adjusted.
2. **Data-quality gate:** trade-critical families (price consensus, derivatives, news ≤48h) present and not `suspect`; two independent sources agree on price direction within 1%.
3. **Structure gate:** stop distance ∈ [1.0×ATR14, 3.5×ATR14] on the plan timeframe and beyond a structure level; R:R to TP1 ≥ 1.5, to blended targets ≥ 2.0.
4. **Sizing (code, not LLM):** `risk_usd = equity × risk_per_trade` (MVP 1.0%; unlocks to 2.0% after 30 trades with PF ≥ 1.3) → `notional = risk_usd / stop_distance_pct` → capped by `max_notional_per_position = 25% × equity × regime_multiplier`.
5. **Leverage (code):** smallest leverage such that margin ≤ allowed, subject to **liquidation distance ≥ 2.5 × stop distance** and ≤ 10x. Isolated margin only.
6. **Portfolio heat:** sum of open risk (distance-to-stop × notional) ≤ 5% equity; gross notional ≤ 150% equity; BTC-beta-adjusted net exposure ≤ 100% equity; max 4 open positions; max 2 same-direction high-beta alts.
7. **Funding gate:** expected funding cost over `time_stop_hours` at current+predicted rate ≤ 25% of expected profit at TP1; else reject or shorten time stop.
8. **Event blackout:** no *new* entries within 6h before FOMC/CPI/NFP-class events unless the plan is tagged `event_thesis`; `EVENT_RISK` regime → limit orders only, half size.
9. **Protections (freqtrade-style):** daily loss ≥ 3% equity → no new entries until next UTC day; weekly loss ≥ 6% → halt + Telegram; 2 stop-outs on same symbol within 5 days → 5-day cooldown; 3 consecutive losses → 24h cooldown & half size for next 3 trades; max 2 new entries per Manager run, 3 per day.
10. **Sanity:** side vs regime posture (short in TREND_UP allowed only with score ≥ +6); no opposite position on same symbol; symbol tradable, min order size, lot rounding.

**Review gates:** `MOVE_SL` only in the profit direction; `PARTIAL_CLOSE` ≥ 25% steps; `CLOSE` on `thesis_status=broken` is mandatory (LLM cannot say HOLD on a broken thesis with negative PnL beyond −0.5R without a gate override reason); time-stop breach → close unless PnL > +1R and trail active.

**Portfolio-level kill-switches (evaluated by Watchdog and Review):** stored in DB per plan (e.g. `BTC daily close > 82,200 → close ARB/ZEC/HYPE shorts`) and globally (equity DD from 30d high ≥ 15% → close all, halt, require manual `/resume`).

Math and worked examples: `DECISION_LOGIC.md` §5.

---

## 8. Layer 5 — Execution & exchange adapter

**Adapter interface** (ccxt-backed; OKX first, Binance/Bybit later):
```
fetch_ohlcv(symbol, tf, limit)          fetch_funding(symbol) / fetch_funding_history
fetch_open_interest(symbol)             fetch_ticker / fetch_order_book(depth)
fetch_balance() / fetch_positions()     fetch_open_orders() / fetch_algo_orders()
set_leverage(symbol, lev, isolated)     place_bracket(plan) -> {entry_id, sl_algo_id, tp_algo_ids}
amend_stop(position, new_px)            place_trailing(position, callback_pct, activation_px)
partial_close(position, pct)            close(position)
cancel_all(symbol)                      reconcile() -> diff(local vs exchange)
```

**OKX specifics (MVP):**
- Account in **net** position mode, **isolated** margin; one position per symbol (invariant enforced by adapter).
- **Entry:** limit order in the plan's entry zone (post-only when regime = CHOP/EVENT_RISK; otherwise limit at zone mid with 0.1% allowance). Market entry only for `urgent=true` plans with spread ≤ 0.03%.
- **Protection at placement:** `attachAlgoOrds` carrying SL (`slTriggerPxType=mark`, market execution) and TP1; OKX now also supports attaching a trailing stop at placement — use it for the runner leg after TP1 when available, else `move_order_stop` via `/trade/order-algo` (your prototype already does this).
- Unfilled entries are cancelled at the next Review run unless the plan's `entry_valid_hours` says otherwise (default 12h).
- **Idempotency:** `clOrdId = f"{run_id[:8]}{plan_id[:8]}"`; before placing, query open orders by clOrdId; never double-place.
- **Reconciliation** at the start of every run: exchange positions/orders are the source of truth; local state is updated; any position without a live SL algo → emergency SL at plan stop (or 2×ATR if no plan) + alert.
- **Rate limits:** ccxt `enableRateLimit`; OKX ~20 req/2s on order endpoints — irrelevant at our volume but keep it.
- **Keys:** trade-only permission (no withdraw), **IP-whitelisted to the VPS** (OKX deletes unbound trade keys after 14 idle days), demo key uses `x-simulated-trading: 1` (never expires).

**Trailing stop policy (non-negotiable, per your decision):** every open position gets a trailing stop, chosen by the Manager per plan (`trail.activation`, `trail.callback_pct`) and **defaulted by code when the plan omits it**: activation when price has moved **+3% in the trade's favour** (or TP1, whichever first), callback `max(1.5 × ATR14%, 2%)`, never below breakeven + fees once active. Implemented as an attached trailing algo at placement where OKX supports it; else placed by the first Review after fill (`move_order_stop`). Watchdog verifies a trailing or fixed SL exists on every position — a position without one is a P1 incident. Full rules: `DECISION_LOGIC.md` §7.

**Insufficient balance / min-size handling:** before placing, the gate computes `required_margin` and checks it against free margin **and** the instrument's minimum contract notional. If either fails, the plan is journaled as `REJECTED: INSUFFICIENT_BALANCE` (with the full dossier, sizing math and the shortfall), a Telegram alert is sent ("plan X skipped: needs $42 margin, $31 free"), the shadow ledger still tracks it, and nothing is placed. A **small-account mode** (`DECISION_LOGIC.md` §5.1) can raise risk-per-trade and leverage inside hard caps when the account is too small for the conservative sizing — your Q2 mechanism.

**Position / order / plan state machines (from the best-practices review):** every object lives in an explicit state with terminal states and a TTL; transitions are atomic SQLite transactions; reconciliation forces stale states to terminal ones. Ghost positions (246 in Chis's case) cannot accumulate because `PENDING_*` states expire and `reconcile()` runs at every run start and every watchdog tick.
```
Plan:      DRAFT → GATED(APPROVED|ADJUSTED) → QUEUED(approve mode) → SUBMITTED → FILLED | EXPIRED | REJECTED | CANCELLED
Order:     NEW → OPEN → PARTIAL → FILLED | CANCELLED | EXPIRED | REJECTED       (TTL = entry_valid_hours; cancel on 2% price drift from zone)
Position:  PENDING_ENTRY → OPEN → REDUCED(partial) → PENDING_EXIT → CLOSED   ↘ ORPHAN (found on exchange without plan → adopted + protected)
Protection: NONE → SL_ATTACHED → TP1_HIT_BE → TRAILING → EXITED             (NONE is only legal for < 1 run; watchdog escalates)
```

**Resilience (from the same review):** per-dependency circuit breakers (exchange, each data source, each LLM provider: closed → open after 5 errors/60s → half-open after 30s); retries with exponential backoff + jitter that **count attempts, not successes** (cooldowns key on attempts); every exchange response validated (`filled_qty > 0`, `price > 0`, symbol matches) before arithmetic; **fees read from the account's fee-rate endpoint and reconciled from actual fills** (never from docs); quantities/prices via ccxt `amount_to_precision` / `price_to_precision` per instrument with `Decimal` arithmetic; absolute paths from config only.

**Modes:** `demo` (OKX demo env) → `auto` (live; your chosen path) with `approve` (live; plans go to Telegram, orders placed only after `/approve`, auto-expire in 90 min) available as a switch — implemented after MVP, but the plan `QUEUED` state and Telegram commands are designed in now so flipping is a config change. Mode is a config flag with a hard interlock: `auto` requires `LIVE_TRADING=I_UNDERSTAND` env var and a non-demo key.

**Second venue:** your **Bybit KZ** key (base URL `https://api.bybit.kz`, ccxt `bybit` with host override) is wired as a **data source from Phase 2** (price consensus, funding/OI cross-check) and as the **execution fallback adapter post-MVP** — same `ExchangeAdapter` interface, so the cost is the host override, a market-map check (Bybit KZ lists a different set than global), and demo testing.

---

## 9. Layer 6 — Telegram (alerts + control; freqtrade-inspired)

Commands: `/status` `/positions` `/plans` `/approve <id>` `/reject <id>` `/close <symbol> [pct]` `/halt` `/resume` `/risk conservative|moderate` `/daily` `/weekly` `/lessons` `/why <trade_id>` (prints the dossier excerpt + scores + gate reasons) `/run manager|review` (manual trigger).
Alerts (event feed): plan proposed/approved/rejected (with gate reasons, incl. `INSUFFICIENT_BALANCE`), fills, SL/TP/trail hits, **every closed position with result (R, $, %, exit reason, time in trade)**, watchdog repairs, kill-switch triggers, data-quality vetoes, LLM fallbacks/consensus vetoes, catalyst confirmations, daily summary (equity, open PnL, day PnL, plans, quota use). `/pnl [today|week|month]` and `/portfolio` give the numbers on demand; the same data backs the web dashboard (`UI_DESIGN.md`).

---

## 9b. Layer 6b — Web dashboard (MVP, read-mostly)

A small FastAPI + HTMX + Tailwind app on the VPS (bound to `127.0.0.1` and reached through `tailscale serve`, which adds TLS and the tailnet's device auth; no app-level auth, and `argus ui` refuses any other bind unless `--allow-remote` is passed) over the same SQLite journal: equity curve and drawdown, open positions with thesis status and protection state, plan pipeline (proposed → gated → filled) with gate reasons, the `/why` trade explorer (dossier → scores → red team → trader → gate → orders → reviews → post-mortem), calibration by score bucket, consensus disagreement stats, data-quality events, LLM quota/latency, kill-switch board, and a *run timeline*. Freqtrade-UI and Nautilus dashboards are references for information architecture only; the visual design is our own (spec, screens and stack in `UI_DESIGN.md`). Built in Phase 4 alongside demo trading so you can review results from day one of demo.

---

## 10. Layer 7 — Journal (SQLite, WAL mode) — the product's real asset

Tables (all rows carry `run_id`, `created_at`, `mode`):
`runs` · `observations` (raw fetch envelopes, compressed) · `dossiers` · `llm_calls` (model, prompt hash, tokens, latency, validation result, raw text incl. reasoning) · `analyses` (scores + evidence) · `redteam_notes` · `plans` (incl. `NO_TRADE` and REJECTED with gate reasons, e.g. `INSUFFICIENT_BALANCE`) · `gate_results` · `consensus_events` · `vettings` (each Trader plan's pass through `runs/pipeline.vet`, blocks included, with the citations the evidence check refused) · `orders` · `fills` (with realised fees) · `positions_snapshots` · `position_events` (state transitions) · `reviews` · `postmortems` · `kill_switches` · `lessons` · `data_quality_events` · `circuit_breaker_events` · `calibration` (score bucket → outcomes) · `shadow_ledger`.

**DB decision:** SQLite (WAL) for MVP — one file, zero ops on a €5 box, `sqlite3 .backup` nightly, plenty for <1M rows/year. Schema uses only ANSI SQL + SQLAlchemy Core so a `pg_dump`-free migration to PostgreSQL is a connection-string change if/when the UI, a second bot instance (stocks) or an MCP over the journal needs concurrent writers. Options weighed in `MEMORY_AND_STATE.md`.

Design rules: append-only for decisions (the *ledger* tables are insert-only with a hash chain — `prev_hash` — so tampering or accidental edits are detectable); every object references the exact `dossier_id` it saw; `replay(run_id)` re-executes layers 3–4 offline against stored dossiers (prompt iteration without new API calls, ~$0). All LLM inputs, reasoning text and outputs are stored in full — the journal is the dataset for every future improvement and for a possible fine-tune.

---

## 11. Layer 8 — Memory & knowledge

- **Static knowledge (`knowledge/`)**: `sources.md` (tiers + traps, seeded from the neighbor chat), `catalysts.yaml` (dated events; each item has `source_url`, `confirmed_by: human|llm`), `tokenomics.yaml` (revenue-to-token mechanics, DefiLlama/CoinGecko slugs, ticker changes e.g. TON→GRAM), `blocklist.yaml`.
- **Lessons memory**: weekly Reflector proposes; a lesson is *active* only with ≥3 supporting closed trades and < 60 days old (reconfirmed or expires). Active lessons (≤10 bullets) are injected into Analyst/Trader prompts. Guards against fitting to noise.
- **Calibration table**: per score bucket (+4, +5, +6, ≥+7) and per factor: win rate, avg R, n. Displayed in `/weekly`; used by the gate to tighten thresholds if a bucket underperforms over ≥20 samples.
- **Per-trade post-mortems** (TradingAgents-style): after close, a ≤120-word verbal feedback grounded in realised return vs BTC over the same window; the last 5 same-asset and 5 cross-asset feedbacks are injected into Analyst/Trader prompts. Cheap, and the Adaptive Multi-Agent BTC paper reports this kind of verbal feedback as the best cost/benefit improvement.
- **Asset memory**: for the core universe, a rolling per-asset card (typical ATR%, funding regime, unlock cadence, past theses and their outcomes) — this is what makes a fixed core set research-efficient (`UNIVERSE_OPTIONS.md`).
- No vector DB in MVP. SQLite FTS5 over theses/lessons/post-mortems is enough for `/why` and Reflector retrieval; embeddings are a documented later option (`MEMORY_AND_STATE.md`).

---

## 12. Scheduling, deployment, cost

**Runtime shape:** one Python package `argus/` with CLI entrypoints (`argus manager`, `argus review`, `argus watchdog`, `argus reflect`, `argus replay <run_id>`, `argus backtest-rules`, `argus ui`). Each run is a **separate short-lived process** started by a **systemd timer** — no long-lived scheduler process to crash or leak. (APScheduler is fine locally; timers are more robust on a €5 box.) The dashboard (`argus ui`) is the only long-lived process and is read-mostly.

**Schedule (UTC):**
- Review: `00:00, 06:00, 12:00, 18:00` (on the 6h/daily candle close so "daily close" kill-switches evaluate on closed candles, and finished well before the manager at :30 — moved from :15 on 2026-10-07)
- Manager: `00:30, 04:30, 09:30, 12:30, 16:30, 20:30` for both venues when armed.
- Watchdog: `*/20`
- Reflect: `Sun 09:30`
- Lock file prevents overlapping runs; Review skips if Manager is mid-execution.

**Hosting:** A small Linux VPS can run the scheduled services. Select capacity, location and access controls for the target environment, and verify deployment and recovery procedures before enabling execution.

**Cost:** Prefer free data and model tiers, with explicitly configured limits on any paid fallback. Actual availability, quotas and pricing vary by provider and account.

**Ops:** Docker optional; `uv` for env; `.env` chmod 600; secrets never in journal; log rotation; healthcheck ping to a free uptime monitor (e.g. healthchecks.io) from each timer; nightly `sqlite3 .backup` to a second location.

---

## 13. Testing & evaluation harness

You cannot cheaply backtest an LLM pipeline end-to-end (look-ahead in model knowledge, no free historical news/X). So evaluation is layered:

1. **Unit tests** for every fetcher parser (recorded fixtures), quality checks (each of the 18 traps has a test), gate rules, sizing math, adapter param building (attachAlgoOrds payloads), idempotency.
2. **Rule-layer backtest** (`argus backtest-rules`): OHLCV + funding history (Binance/OKX via ccxt) → regime filter, screen, ATR/structure stops, TP ladder, time stops, trailing, fees + funding drag. Entries as (a) random-in-screen and (b) simple momentum/mean-reversion proxies. Purpose: prove the *plumbing* (exit + sizing rules) is not negative-expectancy before any LLM alpha is layered on.
3. **Replay harness** (`argus replay`): re-run analysis+gate on stored dossiers with new prompts/models; compare scores and plans across versions (prompt regression tests).
4. **Shadow ledger:** in demo/live, *every* plan — approved, adjusted, rejected, NO_TRADE — is tracked forward with simulated fills at plan prices; produces calibration curves by score bucket and by factor. This is how we learn whether +6 really beats +4.
5. **Demo phase ≥ 4 weeks** on OKX demo with **≥ 50 shadow decisions** (Scira v2 shadow-first gate); **live-small** ($100–200, small-account mode) until 30 closed trades.
6. Metrics: expectancy (R), profit factor, hit rate by bucket, max DD, daily Sharpe, turnover, fee+funding drag %, time-in-trade, % of trades closed by SL/TP/review/time/kill-switch, watchdog repairs count (must trend to 0).

---

## 14. Security & failure containment

- Trade-only API key, IP-bound, sub-account with only the bot's capital; withdraw disabled at exchange level.
- LLM outputs are data, never code or shell; symbol strings validated against the exchange market list before any API call.
- Web content fetched for dossiers is *untrusted*: strip instructions, cap length, never let scraped text set parameters directly (prompt-injection through news is a real vector — the LLM only *scores*, code decides).
- Global halt file (`/var/lib/argus/HALT`) checked by every run; `/halt` writes it; Watchdog writes it on DD breach.
- Every external call has timeout ≤ 20s; total run budget 8 min (alert if a run exceeds it — the "possible infinite loop" monitor); on budget breach → journal partial state, no orders.
- Circuit breakers per dependency (§8); an open breaker on the exchange blocks new entries but never blocks emergency-stop repair attempts.
- **Guardrails:** deterministic Pydantic validators on every LLM output (schema, ranges, side/price consistency, evidence citation, no-widen-stop), plus an input scrub for scraped text; Guardrails AI hub validators are optional add-ons, NeMo Guardrails is not used (`OBSERVABILITY_AND_GUARDRAILS.md`).

---

## 15. Beyond MVP

See `ROADMAP.md` (phases 7+: approve mode, Bybit execution fallback, event-driven wake-up, strategy sleeves from `STRATEGY_STACKS.md`, scout agent for universe changes, trading-tuned open-weight models, journal MCP) and `SIDE_PROJECTS.md` (stocks instance, spot/DCA, options, Polymarket bot — separate bots sharing the ARGUS core and decision pattern).
