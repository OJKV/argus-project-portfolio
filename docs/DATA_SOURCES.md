# DATA_SOURCES.md — API & source catalog (free-first)

**Rule of the file:** tiers reflect *live fetch behaviour* observed in the 6–11 Sep research runs and public API docs as of Sep 2026, not brand reputation. Re-verify limits at build time — free tiers rotate. Each source lists: what signal it feeds, auth, limits, tier, fallback, known traps.

Legend — **T1**: renders real dated data, may drive a trade · **T2**: partial/degraded, corroboration only · **T3**: paywalled/JS-gated/dead, never rely on. **Trade-critical** families: price consensus, derivatives positioning, news ≤48h, catalyst calendar.

---

## 1. Exchange market data & execution

| Source | Signal | Auth / limits | Tier | Notes |
|---|---|---|---|---|
| **OKX v5 public** `/market/candles`, `/market/history-candles`, `/market/tickers?instType=SWAP`, `/public/funding-rate`, `/public/funding-rate-history`, `/public/open-interest`, `/public/mark-price`, `/public/liquidation-orders`, `/market/books` | OHLCV (300/req), funding now+history, OI, mark/index, recent liquidations, spread | none; ~20 req/2s per endpoint | T1 | Primary price source (it's where we fill). History endpoint for >300 bars. |
| **OKX rubik (trading big data)** `/rubik/stat/contracts/long-short-account-ratio`, `/rubik/stat/contracts/open-interest-volume`, `/rubik/stat/taker-volume`, `/rubik/stat/margin/loan-ratio` | Retail L/S ratio, taker buy/sell, OI+volume series | none | T1 | Cheap positioning proxy; use Δ not levels. |
| **OKX v5 private** `/account/balance`, `/account/positions`, `/trade/order` (+`attachAlgoOrds`), `/trade/order-algo`, `/trade/orders-algo-pending`, `/account/set-leverage` | Execution, bracket orders, trailing (`move_order_stop`), algo status | HMAC key; trade-only, IP-bound (unbound trade keys deleted after 14 idle days); demo: header `x-simulated-trading: 1` | T1 | ccxt `okx` with `defaultType: swap`; pass OKX params through `params`. |
| **OKX official MCP/CLI** (`okx-trade-mcp`, `okx-trade-cli`) | Optional: lets Claude Code inspect account/orders during dev | same key | — | Dev tooling only; the bot uses ccxt directly. |
| **Binance USDⓈ-M futures public** `/fapi/v1/klines`, `/fapi/v1/fundingRate`, `/fapi/v1/premiumIndex`, `/futures/data/openInterestHist`, `/futures/data/topLongShortPositionRatio`, `/futures/data/topLongShortAccountRatio`, `/futures/data/globalLongShortAccountRatio`, `/futures/data/takerlongshortRatio`, `/futures/data/basis` | Deep OHLCV history (backtests), OI history, top-trader positioning, taker ratio, basis | none; 1200 weight/min | T1 | Best free history depth; the `/futures/data/*` endpoints only keep ~30 days. Geo-blocking possible on some VPS regions — test. |
| **Bybit v5 public + your Bybit KZ key** `/v5/market/kline`, `/v5/market/funding/history`, `/v5/market/open-interest`, `/v5/market/account-ratio`, `/v5/market/tickers` | Cross-venue price consensus, funding/OI confirmation; **execution fallback post-MVP** | public: none; KZ account: `https://api.bybit.kz` (regional mainnet host; ccxt `bybit` with host override), demo `api-demo.bybit.com` | T1 | Phase 2 as data source. Bybit KZ lists a different market set — always `load_markets()` from the KZ host. |
| **Hyperliquid `info`** (`POST https://api.hyperliquid.xyz/info` type `metaAndAssetCtxs`, `fundingHistory`) | Funding, OI, premium, mark on the largest on-chain perp venue | none | T1 | Good divergence signal vs CEX funding; whale positions only via Lookonchain relays. |
| **Deribit public** `/public/get_book_summary_by_currency`, `/public/get_index_price`, DVOL via `/public/get_volatility_index_data` | Implied vol regime (DVOL), put/call OI skew, max-pain near expiries | none | T1 | Feeds regime (vol) and catalyst (quarterly expiry). |
| **Coinalyze API** `api.coinalyze.net/v1` `/open-interest-history`, `/funding-rate-history`, `/predicted-funding-rate-history`, `/liquidation-history`, `/long-short-ratio-history`, `/ohlcv-history` | *Aggregated* OI, funding, predicted funding, liquidations (long/short split), L/S ratio | free key; **40 req/min** | T1 | Best free aggregate. No timestamps on the *web* pages — but the API returns `t`. Use Δ%, never mix with other vendors in one series. |
| **CoinGlass** | (liquidation heatmap, ETF flows, etc.) | API from $29/mo Hobbyist; web renders empty to fetchers | T3 | Skip. If ever paid, it replaces Coinalyze + Farside. |
| **loris.tools** `/markets/perps/<asset>` | per-venue funding detail | scrape | T2 | Fresh timestamp ≠ correct values (11 Sep ETH page 6% off). Cross-check price vs OKX before trusting OI/funding. |

## 2. Prices, market caps, universe

| Source | Signal | Auth / limits | Tier | Notes |
|---|---|---|---|---|
| **CoinGecko Demo API** `/coins/markets`, `/coins/{id}`, `/coins/categories`, `/global` | Market caps, FDV, categories (sector rotation), supply | free demo key; 30/min, 10k/month | T1 | Coin pages > homepage table. Homepage % column unreliable. |
| **CoinMarketCap** `/v1/cryptocurrency/listings/latest`, `/v2/cryptocurrency/quotes/latest`, `/v1/cryptocurrency/map` | Cross-check prices/caps; trending | free key; 10k credits/mo, 30/min | T1 (cross-check) | Strips +/− signs in some tables; can serve quotes ~24h stale. **Not** the candidate universe (micro-cap bias). |
| **Alternative.me F&G** `api.alternative.me/fng/?limit=30` | Fear & Greed series | none | T1 | Regime input. |
| **Price consensus rule** | — | — | — | Majority of ≥3 of {OKX, Binance, CoinGecko coin page, CMC}; prefer venue prices for anything trade-critical; flag if any source's % contradicts its own 24h range. |

## 3. On-chain, DeFi, fundamentals

| Source | Signal | Auth / limits | Tier | Notes |
|---|---|---|---|---|
| **DefiLlama** (⚠️ 2026-09-17: `/emissions` and `bridges.llama.fi` are now **paid** — F-048) `api.llama.fi/v2/chains`, `/v2/historicalChainTvl/{chain}`, `/protocol/{slug}`, `/overview/fees?…`, `/summary/fees/{slug}`, `/stablecoins` (`stablecoins.llama.fi`), `/emissions` (unlocks), `/overview/dexs`, `/bridges` | TVL Δ, fees/revenue Δ, stablecoin supply Δ (liquidity), unlock schedule, DEX volumes, bridge flows | none | T1 | Slug map lives in `knowledge/tokenomics.yaml`, verified monthly (ticker/slug changes). Unlock data is extrapolated — corroborate with Tokenomist/CryptoRank. |
| **Tokenomist** `tokenomist.ai/<project>`, **CryptoRank** unlock pages | Next unlock date/size; full split paywalled | scrape | T2 | Recipient class contested across sources — record the precedent (last identical unlock) as evidence. |
| **mempool.space API** `/api/v1/fees/recommended`, `/api/v1/mining/hashrate/3d`, `/api/mempool` | BTC fee pressure, hashrate/difficulty (miner stress) | none | T1 | |
| **blockchain.info charts** `api.blockchain.info/charts/<name>?format=json` | hash-rate, tx count, misc BTC series | none | T1 | |
| **Etherscan V2** `api.etherscan.io/v2/api?chainid=…` | Gas, large transfers of tracked wallets (curated exchange wallets), token supply | free key; **3 req/s, 100k/day, selected chains only** (verified 2026-10-05) | T1 | **No fetcher written yet — zero calls today.** Curated wallet list in `knowledge/wallets.yaml`. Limit pre-registered in `argus/data/ratelimit.py`; see §3.1 before writing the fetcher. |
| **Dune API** (`/api/v1/query/{id}/results`) | Exchange netflow / whale cohort queries (saved queries) | free tier includes API; limited executions/credits | T2 (until queries proven) | Web dashboards render titles only — use API. |
| **Lookonchain token feeds** `lookonchain.com/token/bitcoin|ethereum|solana|…`, `/feeds/<id>` | Whale/exchange transfers, HL whale positions, in prose | scrape (text) | T1 | Homepage is stale cache; use token feeds. Attribution claims (e.g. "wallet = X") are unverified unless signed. |
| **bitcointreasuries.net** | Corporate BTC holdings | scrape | T1 | |
| **Santiment SanAPI** (GraphQL) | Social volume, dev activity, exchange flow (limited metrics on free) | free tier, restricted metrics/history | T2 | Insight list renders, metrics often gated — evaluate in build week 3. |
| **Glassnode** | SOPR, MVRV, exchange netflow (Tier-1 metrics only, 24h res on free) | free account; full API paid | T2 | Weekly research posts (`research.glassnode.com`) render as text — usable as evidence. |
| **CryptoQuant / Nansen / Arkham / Whale Alert / Messari / Token Terminal** | — | paid / gated / empty | T3 | Do not build on. Arkham API $19.99/mo is the cheapest *if* whale attribution becomes a proven factor. |
| **GitHub REST** `/repos/{owner}/{repo}/stats/commit_activity` | Dev activity 30d/90d | free (60/h unauth, 5k/h token) | T1 | Repo map in `tokenomics.yaml`. |
| **DefiLlama Bridges** `bridges.llama.fi/bridges`, `/bridgevolume/{chain}`; **LayerZero Scan API**, **Socket API** | Bridge inflow/outflow per chain (capital rotation between ecosystems) | none (LayerZero/Socket: free keys) | T1 (DefiLlama) / T2 (others) | DefiLlama bridge volumes cover the signal for our horizon; LayerZero/Socket add message-level detail we don't need in MVP. |
| ~~**Dune**~~ — **REMOVED 2026-09-19 (F-066).** The 14-day trial has API credits, but the plan it reverts to is view-only, and free executions are never supported via the API on any plan. No fetcher was written. | — | — | — | On-chain coverage comes from Etherscan (v2 multichain), DefiLlama and mempool.space, all free and keyless or free-keyed. |

### 3.1 Etherscan plan rules — read before writing the fetcher (2026-10-05)

Etherscan published four changes between 1 Oct and 1 Nov 2026. None affect ARGUS today, because
**no fetcher calls Etherscan** — the key exists in `Settings` and `doctor` checks that it is
present, nothing more. They do bind the first fetcher that is written:

| Change | Effective | What it means for us |
| --- | --- | --- |
| Free tier is **3 calls/s, 100k/day, selected chains only** | now | Registered as `api.etherscan.io: (2, 1)` in `argus/data/ratelimit.py`. The generic default is 5/s, which would have breached the ceiling silently. Regression test: `tests/data/test_cache_ratelimit.py`. |
| Daily cap **enforced**; requests above it blocked unless "Allow Overage Charges" is on ($0.00003/call) | 2026-11-01 | **Leave overage off.** A blocked call is a `data_gap` the envelope already models; enabling it turns a free source into a metered one and needs a cost line under non-negotiable #5. At 15 assets × 6 manager runs/day a realistic budget is in the hundreds of calls/day against 100k — the cap is not the binding constraint, the per-second rate is. |
| ToS: Free/Lite/Standard are **personal & research use only**; Advanced+ for commercial apps | 2026-10-01 | Review the applicable terms for the intended use before enabling the source. |
| opBNB mainnet/testnet endpoints deprecated; Robinhood & Arc move to Lite-only | 2026-10-12 / 10-16 | We query none of these chains. `wallets.yaml` is Ethereum-and-majors only. |

Budget rule for the fetcher when it lands: one call per tracked wallet per manager run, cached 4 h
(§3 cadence), and the token bucket above is the hard stop — never a per-fetcher sleep.

## 4. Macro, rates, ETF flows, event odds

| Source | Signal | Auth / limits | Tier | Notes |
|---|---|---|---|---|
| **FRED API** series `DGS10`, `DFII10` (10Y TIPS real yield), `DTWEXBGS` (broad DXY), `FEDFUNDS`, `T10Y2Y`, `VIXCLS` | Real-yield tripwire (>2.50% risk-off in the neighbor method), DXY Δ, curve | free key; 120 req/min | T1 | Lags 1 day; fine for our horizon. |
| **Fed H.15** `federalreserve.gov/releases/h15/` | Authoritative daily yields incl. TIPS | scrape | T1 | |
| **Cleveland Fed inflation nowcast** | Highest-value CPI input; beat street twice in Sep | scrape; check "as of" | T1 | |
| **Investing.com Fed Rate Monitor**, **centralbank.watch** | Hike/hold odds (CME-derived), futures-implied path | scrape | T1 / T2 (lags) | |
| **Polymarket Gamma API** `gamma-api.polymarket.com/markets?…`, `/events` | Fed odds, BTC price markets, regulatory votes (e.g., CLARITY) | none | T1 | Gap vs futures-implied odds is itself a signal. Also feeds your Polymarket picks later. |
| **Kalshi API** (`api.elections.kalshi.com/trade-api/v2/markets`, public market data) | Regulated US event contracts: Fed decision, CPI prints, BTC price ranges | none for market data | T1 | Cross-check Polymarket; the Polymarket↔Kalshi↔futures spread is a cleaner "crowd disagreement" signal than any single venue. |
| **FinFeedAPI** (9+ prediction markets aggregated) | Aggregated event odds | limited free | T2 | Evaluate only if Polymarket+Kalshi coverage proves insufficient. |
| **ForexFactory calendar JSON** `nfs.faireconomy.media/ff_calendar_thisweek.json` | Weekly high-impact events (CPI, NFP, FOMC, PPI) | none | T1 | Drives the event blackout gate. |
| **BLS API v2** (`api.bls.gov`), **Investing.com calendar** | CPI/PPI/payroll actuals & consensus | free key; scrape | T1 | Verify consensus on two sources (trap #16). |
| **TradingEconomics pages** (bond yield, TIPS, DXY, oil, gold) | Cross-check macro levels | scrape (guest API very limited) | T1 | |
| **Farside** `farside.co.uk/btc/`, `/eth/` | Per-issuer ETF flows | scrape | T1 | **Blank ≠ 0; "Total 0.0" on a blank row is a rendering default.** Corroborate with WuBlockchain/PANews. |
| **NY Fed SCE** | Inflation expectations | scrape | T2 | Often serves last month's data. |

## 5. News, social, search

| Source | Signal | Auth / limits | Tier | Notes |
|---|---|---|---|---|
| ~~**CryptoPanic**~~ — **REMOVED 2026-09-19.** Confirmed paid-only; the free endpoint returned HTTP 403 (F-044). No `CRYPTOPANIC_API_KEY` is needed and none is used. | — | — | — | The news family is RSS-only: Google News per symbol plus CoinDesk, The Block, Cointelegraph and Decrypt — all keyless, all verified 6/6 on 2026-09-19. |
| **Google News RSS** `news.google.com/rss/search?q=<query>+when:2d` | Per-symbol/per-narrative headlines with publisher + date | none | T1 | Hard date filter; drop >7d. |
| **Primary outlets** (The Block, Cointelegraph, Decrypt, Blockworks, DL News, PANews, Bloomingbit, WuBlockchain; Reuters/CNBC/Axios for macro) | Verification of CryptoPanic hits | fetch article text via **Jina Reader** `r.jina.ai/<url>` (free tier) | T1 | ~⅓ of on-chain search hits were 6–12 months old presenting as current — always compare article date to claim date. |
| **Scira API** `POST api.scira.ai/api/search` **(payload shape UNVERIFIED — F-067: `{"query": …}` returns 400, `{"messages": […]}` returns 500; get the real schema from their docs before writing the fetcher)** (web + AI summary + sources), `/api/xsearch` (X posts with engagement, optional `username`), `/api/reddit` (Reddit threads + summary), `/api/people` | Open-ended "dynamic" search (your note), X narrative velocity, Reddit sentiment | `x-api-key`; **Free 20 req/day, Pro 1,000/day**, resets 00:00 UTC; 429 on excess | T1 (as *evidence pointer*, verify sources) | Budget: 2 `/search` (macro + "what changed today"), up to 6 `/xsearch` (top-3 candidates × 2 queries), 2 `/reddit`, reserve 10. The `results[].tweet.favorite_count`/`conversation_count` fields give a cheap "narrative velocity" feature. Model summaries are *not* evidence — the `sources[]` are; fetch & date-check them. |
| **Reddit API** (OAuth, 100 QPM) | r/CryptoCurrency, r/CryptoMarkets, per-asset subs | free app | T1 | Alternative to Scira `/reddit` if quota is tight. |
| **X API** | — | paid; free read tier dead | T3 | Use Scira `/xsearch`. |
| **Brave Search API / Tavily / Exa** | Fallback web search | free tiers (Brave 2k/mo; Tavily 1k/mo; Exa credits) | T1 | Only if Scira quota exhausted. |
| **LunarCrush / Kaito / The Block data** | social/mindshare | paid | T3 | Skip. |
| **CoinStats MCP** (20+ tools: prices, news, portfolio, DeFi) | News + prices via MCP; "agent-native" convenience | free tier (~20K credits/mo) | T2 (evaluate) | Phase 2 evaluation: if its news tool beats CryptoPanic+RSS on freshness, keep as a fetcher; never on the execution path. |

## 6. SEC filings & crypto-treasury equities

| Source | Signal | Auth / limits | Tier | Notes |
|---|---|---|---|---|
| **EDGAR full-text search** `efts.sec.gov/LATEST/search-index?q="bitcoin"&forms=8-K&dateRange=custom&startdt=…` | New 8-Ks mentioning BTC purchases/sales, ATM raises, convertible notes (MSTR, COIN, MARA, RIOT, CLSK, SMLR, HOOD, GME, MTPLF) | none; **User-Agent `name email` required**; ≤10 req/s | T1 | `www.sec.gov/cgi-bin/browse-edgar` is robots-blocked to automated fetch — use `efts` + `data.sec.gov/submissions/CIK##########.json`. Parse "Item 8.01"/"Item 2.02" text via Jina Reader on the primary doc URL. |
| **bitcointreasuries.net** | Holdings deltas confirm 8-K parsing | scrape | T1 | |
| **sec-edgar-mcp** (Stefano Amorelli; EdgarTools-based; free; an MCP) | Filings lookup, XBRL facts, full-text search via MCP tools | none (User-Agent) | T1 | **Preferred over a hand-written EDGAR fetcher**: `argus` runs it as a stdio MCP server from the Manager run (Pydantic AI MCP client) with a 20s timeout and falls back to the raw `efts` fetcher if the server fails. |
| **Unusual Whales API** | Equity options flow, congressional trades | paid ($) | T3 | Out of scope for crypto perps; relevant to the stocks side project. |

## 6b. Exchange MCP servers (dev tooling, not execution path)

| MCP | What | Use in ARGUS |
|---|---|---|
| **OKX `okx-trade-mcp` / `okx-trade-cli`** (official) | Market data, orders, algo orders, account, bots | Claude Code inspects demo/live account state during development and incident response; the bot trades via ccxt |
| **Binance Agent OS MCP** (official, OAuth: `agent.binance.com/mcp/agentic`) | Read account/positions/orders, place/cancel — no API key, OAuth session | Same dev-tooling role when Binance is added; OAuth sessions are unsuitable for an unattended cron bot |
| **Bybit** | community MCP servers only | Skip; ccxt |

## 7. LLM providers (free tiers, Sep 2026 — re-verify monthly; full router plan in `LLM_PROVIDERS_AND_MODELS.md`)

| Provider | Models (typical) | Free limits | Role in router |
|---|---|---|---|
| **Google AI Studio** | gemini-2.5-flash (stable), gemini-3-flash (preview), flash-lite | ~10 RPM / ~250 RPD (Flash); ~1,000 RPD (Flash-Lite); resets 00:00 PT; per-project. Pro no longer free. **Enabling billing on the project deletes its free tier** — keep a separate free project. | Primary for Analyst/Trader (long context, good JSON) |
| **Groq** | llama-3.3-70b-versatile, qwen3-32b, gpt-oss-120b | ~30 RPM, ~1,000 RPD (varies by model) | Primary for Reviewer/Regime; consensus second opinion |
| **OpenRouter `:free`** | deepseek-r1 / qwen3 / gpt-oss / nemotron variants (rotate) | 20 RPM; **50 RPD unfunded, 1,000 RPD after a one-time ≥$10 credit purchase** | Fallback + reasoning-heavy Red Team |
| **Mistral Experiment** | mistral-large / medium | 1 RPS, ~1B tokens/month; prompts may be used for training | Fallback |
| **Cerebras** | llama / qwen / gpt-oss | ~1M tokens/day (now finite-trial in some accounts — verify) | Optional fallback |
| **NVIDIA NIM** (`integrate.api.nvidia.com`) | 40+ models incl. GLM-5.x, Nemotron, Qwen3.x, DeepSeek | ~40 RPM, no card | **Add** — deepest free catalog; strong reasoning tier |
| **Cloudflare Workers AI** | Llama 3.3 70B, Qwen, Mistral | 10K neurons/day | **Add** — fallback tier |
| ~~GitHub Models~~ | — | retired 2026-07-30 | **Removed** 2026-09-17 (FAILURE_LOG F-044) |
| **SambaNova** | Llama, DeepSeek, Qwen | ~200K tokens/day per model | Add |
| **Cohere** | Command-A | 1K req/month, non-commercial | Skip (licence) |
| **Z.ai GLM** (international) | `glm-4.7-flash`, `glm-4.5-flash` | free for registered accounts | **Add** — use `api.z.ai`, not `open.bigmodel.cn` |
| **DeepSeek (paid, every agent's last resort, first)** | `deepseek-flash`, `deepseek-v4-pro` (`api.deepseek.com`, `DEEPSEEK_API_KEY`) | none; paid. Read 2026-10-26 from `api-docs.deepseek.com/quick_start/pricing` (cache-miss input / output, USD per 1M): flash $0.15 / $0.60 off-peak, **$0.30 / $1.20 peak**; v4-pro $0.66 / $1.98 off-peak, **$1.32 / $3.96 peak** (peak = 01:00-04:00 and 06:00-10:00 UTC Mon-Fri). `config/models.yaml` `prices` holds the peak rates so the cap never under-counts | **Approved by owner 2026-10-26, for every agent** (`paid_last_resort: true` on all ten): tried after an agent's whole free chain has failed (Gemini 503 twice in a day lost a manager slot), before OpenAI. Hard-capped at **$5/month for DeepSeek alone** (`PAID_MONTHLY_CAP_USD_BY_PROVIDER`), inside a $10 overall ceiling (`PAID_LLM_MONTHLY_CAP_USD`); at its cap the router stops offering DeepSeek and keeps offering OpenAI. A judge (red team, consensus, reviewer) never gets the Trader's own paid family |
| **OpenAI (paid, every agent's last resort, second)** | `gpt-5-mini`, `gpt-5-nano` (`api.openai.com`, `OPENAI_API_KEY`) | none; paid. Read 2026-10-26 from `developers.openai.com/api/docs/pricing.md` (Standard, text, short context, USD per 1M in / out): gpt-5-mini $0.25 / $2.00; gpt-5-nano $0.05 / $0.40 | **Approved by owner 2026-10-26, for every agent**: backup after DeepSeek, under its **own $5/month cap** (spend on one provider never uses up the other's) and the same $10 overall ceiling. Reasoning models: output budget (`max_tokens` 2000) not yet checked against the live API |

Router policy: `LLM_AGENTS.md` §5 and `LLM_PROVIDERS_AND_MODELS.md`. Budget ≈30–40 calls/day (ceiling 100) across 8+ providers; the chain exists for 429s and model deprecations (your prototype's `gemini-2.5-flash` will not exist forever).

## 8. Infrastructure

A Linux host, backups, alerting and an uptime monitor can support the scheduled services. Select and review the provider, security controls and recovery procedures for the target environment.

---

## 9. Known data-quality traps (mechanised as tests; seeded from `SOURCES-crypto-signals.md` v1.1)

1. CoinGecko homepage % column contradicts its own coin pages → use coin pages / venue API.
2. Sanity-check every % against its 24h range (spot above stated high ⇒ stale).
3. Farside blank ≠ zero; "Total 0.0" on a blank row is a render default.
4. CMC strips +/− signs; may serve ~24h-stale quotes.
5. Subscript-zero notation flattens (PEPE "$0.053632" = $0.0₅3632).
6. Nonsense volume fields (ARB "$342B") → discard the whole row.
7. Coinalyze *web* price columns stale → use API `t`, and only Δ%.
8. Liquidation totals contradict across outlets → prefer per-asset Coinalyze; aggregate totals are soft.
9. OI levels differ by vendor → Δ% only, one vendor per series.
10. Recirculated stale articles (~⅓ of on-chain search hits) → hard date filter.
11. Ticker/slug changes (TON → GRAM) → verify mappings monthly.
12. Aggregator "catalyst calendars" get dates wrong (TOKEN2049) → primary source only.
13. Notional ≠ loss in headlines.
14. Unverified wallet attributions.
15. loris.tools: fresh stamp with corrupted values → cross-check price vs OKX first.
16. Consensus figures misreported → two sources before framing a print.
17. Aggregators disagree on a major's price *and direction* by ~2% → majority of 4, venue-first.
18. Blank Farside row rendering "Total 0.0" (again, confirmed 10 Sep).

Every trap ⇒ one unit test in `tests/data_quality/` and one `data_quality_events` row when triggered in production.


---

## 10. Verdict on every API named in your notes (2026-09-14)

| Named | Verdict | Where |
|---|---|---|
| DeFiLlama | **Core, T1** | §3, bridges added |
| Arkham | T3 (API $19.99/mo; web gated) — only if whale attribution becomes a proven factor | §3 |
| Glassnode | T2 — research posts as text; free T1 metrics if the free API key works (verify Phase 2) | §3 |
| CryptoQuant | T3 (API $29+; web gated) — skip | §3 |
| Santiment | T2 — free SanAPI metrics restricted; evaluate | §3 |
| LookOnChain | **T1 via token feeds** (text scrape) | §3 |
| Scira (Web/X/Reddit) | **T1 evidence pointer**, free 20/day rationed | §5 |
| CryptoPanic | **Not used.** Paid-only as of 2026-09; the key may stay in `.env` but nothing reads it. |
| LunarCrush | T3 paid — skip | §5 |
| Fear & Greed | **T1** | §2 |
| CoinMarketCap | T1 cross-check only | §2 |
| CoinGecko | **T1 (demo API)** | §2 |
| Messari | T3 (403s) — skip | §3 |
| Token Terminal | T3 (labels only) — skip; DefiLlama fees/revenue cover it | §3 |
| FRED | **Core, T1** | §4 |
| SEC EDGAR / EDGAR MCP | **T1 via sec-edgar-mcp**, raw `efts` fallback | §6 |
| TradingEconomics | T1 pages (scrape), API guest-only | §4 |
| CoinGlass | T3 free / $29 paid — replaced by Coinalyze + exchange-native | §1 |
| Deribit | **T1** (DVOL, put/call, expiries) | §1 |
| Polymarket | **T1** | §4 |
| Kalshi | **T1, added** | §4 |
| FinFeed | T2 — only if needed | §4 |
| Mempool & tx monitoring (mempool.space; Blocknative/Alchemy) | mempool.space **T1** for fee/miner stress; Blocknative/Alchemy mempool streams are MEV/short-horizon tooling — out of scope for 1–7d | §3 |
| Unusual Whales | T3 paid, equities — stocks side project | §6 |
| Cross-chain (LayerZero, Socket, DeFiLlama bridges) | DefiLlama bridges **T1**; others T2 later | §3 |
| Binance / OKX / Bybit MCPs, Binance Agent OS | Dev tooling only; execution stays on ccxt | §6b |
| Bybit KZ key | **Data source Phase 2; execution fallback post-MVP** | §1 |
