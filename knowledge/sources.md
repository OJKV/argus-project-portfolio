# Standard Sources — Crypto Signal Research

**Purpose:** the standing source list for daily crypto signal runs. Not a limit — each run also searches beyond it — but this is the reliable core, plus the known traps.
**Version:** v1.2 · created 10 Sep 2026 · last updated 14 Sep 2026 · updated every run.

Reliability tiers are based on repeated live testing, not reputation. A famous analytics brand that renders an empty page is Tier 3 here.

---

## TIER 1 — Renders real, dated data. Use as primary.

### Prices & market structure
| Source | URL | Notes |
|---|---|---|
| CoinGecko coin pages | `coingecko.com/en/coins/<slug>` | **Primary price source.** Always use the individual coin page |
| CoinMarketCap coin pages | `coinmarketcap.com/currencies/<slug>/` | **Cross-check only.** Resolves CoinGecko errors |
| Binance / Bybit price pages | `binance.com/en/price/<asset>`, `bybit.com/en/price/<asset>/` | Venue-level highs/lows; essential when spot aggregates disagree with your perp fill |
| CoinGecko categories | `coingecko.com/en/categories` | Sector rotation |

### On-chain & whales
| Source | URL | Notes |
|---|---|---|
| bitcointreasuries.net/news | `bitcointreasuries.net/news` | Dated corporate-treasury news items — added v1.2 |
| TechFlow | `techflowpost.com/en-US/newsletter/<id>` | Best Hyperliquid position-delta relay found; exact sizes and timestamps — added v1.2 |
| BeInCrypto | `beincrypto.com` | Full whale-position tables with addresses, entries, liquidation levels — added v1.2 |
| **Lookonchain token feeds** | `lookonchain.com/token/bitcoin`, `/ethereum`, `/solana`, `/zcash`, `/bnb`, paginated `/2` | **Best whale source that actually renders.** Use token feeds, NOT the homepage (homepage serves stale cache) |
| Lookonchain feed items | `lookonchain.com/feeds/<id>` | Individual items render fully |
| DefiLlama | `defillama.com` + `/chains`, `/fees`, `/stablecoins`, `/protocol/<name>`, `/chain/<name>` | Fees, revenue, TVL, stablecoin supply. Fully reliable |
| Bitcoin Treasuries | `bitcointreasuries.net` | Corporate BTC holdings, live |
| Glassnode research | `research.glassnode.com` | Weekly "Market Pulse". Article text renders; charts do not |
| Santiment insights | `app.santiment.net/insights` | Insight list renders; metrics do not |
| mempool.space | `mempool.space` | Fees, difficulty estimate, mempool — the best free miner-stress proxy |

### Derivatives
| Source | URL | Notes |
|---|---|---|
| Coinalyze | `coinalyze.net` + `/<asset>/funding-rate/`, `/open-interest/`, `/liquidations/` | Best free funding/OI/liquidations. **No timestamps; self-disclaims as not real-time.** Use Δ%, not price columns |
| loris.tools | `loris.tools/markets/perps/<asset>` | Per-venue funding detail. **Staleness varies 0–25h per page — always read the printed "Last Updated"** |

### Flows
| Source | URL | Notes |
|---|---|---|
| Farside | `farside.co.uk/btc/`, `/eth/` | Per-issuer ETF flows. **Blank cell = not yet reported, NEVER zero** |
| WuBlockchain | `wublock123.com` | Publishes SoSoValue ETF data faster than Farside |
| PANews / Bloomingbit | `panews.io`, `en.bloomingbit.io` | Asian aggregators — fastest ETF and whale relays |

### Macro
| Source | URL | Notes |
|---|---|---|
| Cleveland Fed nowcast | `clevelandfed.org/indicators-and-data/inflation-nowcasting` | **Highest-value macro input.** Check the "as of" stamp every run |
| Investing.com Fed Rate Monitor | `investing.com/central-banks/fed-rate-monitor` | CME-derived hike/hold odds with a day-ago comparison |
| centralbank.watch | `centralbank.watch/federal-reserve/` | Futures-implied path. Can lag a few days |
| Polymarket | `polymarket.com/event/<slug>` | Fed odds, crypto price markets. Often diverges from futures — that gap is signal |
| TradingEconomics | `tradingeconomics.com/united-states/government-bond-yield`, `/10-year-tips-yield`, `/commodities`, `/currencies`, `/stocks` | Yields, **10Y TIPS real yield**, DXY, oil, gold, equities |
| Fed H.15 | `federalreserve.gov/releases/h15/` | Authoritative daily yields incl. TIPS |
| BLS | `bls.gov/news.release/cpi.nr0.htm`, `/ppi.nr0.htm`, `/empsit.nr0.htm` | Primary CPI/PPI/payrolls |
| Investing.com calendar | `investing.com/economic-calendar/` | Consensus figures per release |
| NY Fed SCE | `newyorkfed.org/microeconomics/sce` | ⚠️ Page often serves last month's data — verify against wire reports |

### News
**CryptoPanic (`cryptopanic.com`) — standing first stop for news.** Aggregated, fast, with sentiment/vote signal; use it to find the story, then open the primary outlet to verify.
Then: CoinDesk · The Block · Cointelegraph · Decrypt · Blockworks · crypto.news · Bitcoin.com News · DL News · AMBCrypto · BeInCrypto · Coinpedia · CryptoPotato · CryptoTimes · U.Today · CryptoDnes
Macro/geopolitics: Al Jazeera · Reuters · CNBC · CBS · PBS · Axios · Kiplinger · FXStreet

### Tokenomics & unlocks
Tokenomist (`tokenomist.ai/<project>`) — free tier shows next date, **full splits paywalled** · CryptoRank · DefiLlama unlocks (**explicitly extrapolates and disclaims accuracy**) · protocol docs and governance forums

---

## TIER 2 — Partial or degraded. Use with care.
- **Lookonchain homepage** — stale vs its own token feeds
- **ultrasound.money** — supply renders, burn rate loads async
- **Token Terminal** — labels only, no values
- **Dune** — dashboard titles only, zero metric values
- **CoinGecko gainers/losers** — hard-scoped to top 1000, dominated by micro-caps, and **has repeatedly omitted genuine large-cap movers**. Screen via coin pages instead
- **Messari** — mostly 403

## TIER 3 — JS-gated, paywalled or dead. Do not rely on.
CoinGlass (renders empty — report "no data", never "$0") · Hypurrscan · Hyperdash · data.asxn.xyz · Whale Alert (paywall) · SpotOnChain (SEO shell) · Arkham / intel.arkm.com · Nansen (paywall) · CryptoQuant (chart pages gated) · The Block /data (nav only) · taostats.io (shell) · onchainlens.com (**robots.txt disallowed**) · SoSoValue (marketing copy) · strategy.com (empty dashboard shell) · SEC EDGAR (robots-blocked to automated fetch)

**For Hyperliquid-native data specifically:** every native tracker is unreachable without a browser. Lookonchain's feed, PANews and BeInCrypto are the only text-accessible relays.

---

## Known data-quality traps (each one has actually bitten a run)

1. **CoinGecko homepage % column is unreliable** — repeatedly contradicts its own coin pages. Always use coin pages.
2. **Sanity-check every % against its 24h range.** If spot sits above the stated 24h high, or a stated +2% is impossible inside the range, the field is stale. This has caught errors on LINK, PUMP, ZEC and BTC.
3. **Farside blank ≠ zero.** The single most likely misread in ETF data.
4. **CoinMarketCap strips +/− signs** in some fetched tables, and sometimes serves quotes ~24h stale.
5. **Subscript-zero notation flattens.** PEPE "$0.053632" is really $0.0₅3632 = $0.00000353. Same for other micro-priced tokens.
6. **Nonsense volume fields** — CoinGecko has printed ARB volume as "$342B". Ignore volume from a page whose other fields are wrong.
7. **Coinalyze price columns go stale** (~$25 wrong on BNB in one run) — enough to make a stop-out look like it never happened. Use it for funding/OI Δ only.
8. **Liquidation totals contradict across outlets**, sometimes with the same figure on opposite sides of the long/short split. Prefer per-asset Coinalyze; treat aggregate totals as soft.
9. **OI levels differ by vendor** (venue coverage, not error). Use Δ%, never mix vendors in one series.
10. **Recirculated stale articles.** Roughly a third of search results for on-chain queries were 6–12 months old while presenting as current: a Tether $2B mint (Sept 2025), a CryptoQuant whale-ratio piece (Feb 2026), a QCP note (Jan 2025). **Apply a hard date filter to every claim.**
11. **Ticker/slug changes.** TON now serves as **GRAM** on CoinGecko. Verify mappings before automated routing.
12. **Conference and event dates in aggregator "catalyst calendars" are often wrong.** TOKEN2049 Singapore was listed as Sept 16–18 by one such site; the official site says **7–8 October**. Verify against the primary source.
13. **Notional ≠ loss.** Headlines about the ZEC whale's "$47M short" quoted position size as if it were the loss (~$22M at the time).
14. **Unverified attributions.** The ZEC Hyperliquid wallet is widely labelled "Garrett Jin" on one analyst's say-so, with no signed message or filing.
15. **loris.tools can serve a fresh timestamp with corrupted values.** On 11 Sep its ETH page was stamped 14:19 UTC (current) but printed $2,610 against three sources at ~$2,465 — ~6% high — and its OI change mirrored the bogus price move. **A current timestamp is necessary but not sufficient: cross-check the price against CoinGecko before trusting the OI/funding on the same page.** Its BNB page was 26 days stale on the same day.
16. **Consensus figures themselves get misreported.** Street core CPI m/m consensus was +0.2%, but one source carried +0.4% — which inverts whether a print reads as hot or cool. **Verify consensus on two sources before framing a print.** The Cleveland Fed nowcast has beaten the street twice running.
17. **Two aggregators can disagree on a major asset's price by ~2% AND on its direction.** On 11 Sep: CoinGecko $78,557/+1.6% vs CMC $77,108/−3.03% vs Bybit $76,802 vs Binance $76,814, all passing their own in-range checks. **Take the majority of 4 sources, and prefer venue pages for anything trade-critical.**
19. **CoinGecko can serve a SIGN-FLIPPED percentage.** On 14 Sep its ARB 30d field read **−84.1%** when the true value was **≈ +83%** (Coinbase +83.81%, Bybit +82.66%). The page contradicted itself — it simultaneously claimed ARB was +95.6% above an all-time low that a −84% month would put it far below. **Any automated ingest of `price_change_percentage_30d` needs a sign/sanity guard: cross-check magnitude against a second venue and against the stated distance from ATL/ATH.**
20. **Lookonchain `/token/<asset>` pages can serve a broken cache.** On 14 Sep `/token/zcash` returned items stamped Sept 16–17 2026 (future-dated) alongside Dec 2024–Jan 2025 items. The BTC/ETH/HYPE feeds were fine the same day. **Check the date on every item; a token feed is not uniformly reliable just because another one is.**
21. **Lookonchain `/feeds/<id>` IDs are NOT chronological.** `/feeds/37270` is a November 2024 item about a different wallet entirely. Always verify the date inside the item.
22. **Official macro data gets revised, and intraday quote pages lag it.** A 10Y TIPS real yield captured as 2.47% for Sept 10 was superseded by the official **H.15 value of 2.55%** — enough to flip a threshold-based kill switch from "not triggered" to "triggered". **For any number that gates a decision, prefer Fed H.15 / FRED / BLS over quote aggregators, and re-check the prior day's value on the next release.**
23. **A blank Farside row can show "Total 0.0"** — that 0.0 is a rendering default, not a flow. Confirmed again 10 Sep when actual BTC flows were −$283M.

---

## Method rules for every run
- Tag every figure with **what was observed directly** vs **what an article claimed**, and the date it refers to.
- Two independent sources for any number that drives a trade decision.
- Prefer the more rigorous metric when sources conflict (e.g. Glassnode's Accumulation Trend Score over a single X analyst's cohort read).
- Report what could NOT be verified — gaps change position sizing.
- Log any new trap discovered into this file at the end of the run.

## Changelog
- **v1.2 (14 Sep 2026):** Added traps 19–22 (CoinGecko sign-flipped 30d percentage; Lookonchain token-feed broken cache; non-chronological feed IDs; official macro revisions superseding intraday quotes). Added bitcointreasuries.net/news, TechFlow and BeInCrypto to on-chain Tier 1/2. Demoted Lookonchain `/token/zcash` to Tier 2 (date-check required). Confirmed live this run: Lookonchain BTC/ETH/HYPE feeds, DefiLlama chains+stablecoins, bitcointreasuries, mempool.space, Farside, research.glassnode.com (index only — Week-37 permalink 404s), H.15, FRED, TradingEconomics, Polymarket, centralbank.watch, CryptoPanic.
- **v1.1 (11 Sep 2026):** CryptoPanic promoted to standing first stop for news. Added traps 15–18 (loris fresh-timestamp-but-corrupt-values; misreported consensus figures; cross-aggregator price AND direction conflicts; Farside blank rendering as "Total 0.0"). Added CryptoPotato, CryptoTimes, CryptoDnes to news list. Confirmed live this run: Lookonchain token feeds, DefiLlama, Farside, PANews, BLS, H.15, FRED, TradingEconomics, Investing.com Fed Monitor, Polymarket, Glassnode research, loris (per-page, timestamp-dependent).
- **v1.0 (10 Sep 2026):** created from source testing across the 6–10 Sep runs. Tier assignments reflect live fetch results, not brand reputation.
