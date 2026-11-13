# STRATEGY_STACKS.md — signal-generation stacks: what's already a factor, what becomes a sleeve, what is a side project

Your note lists four "stacks" from the best-practices doc and asks whether they make sense and what else to add. Frame: ARGUS is a **platform** (data layer, gate, execution, journal) that can run several **sleeves** — each sleeve is a decision recipe with its own trigger, universe, horizon and risk budget. The five-factor swing sleeve is MVP. A **meta-allocator** (post-MVP) splits the risk budget across sleeves by their rolling expectancy.

## 1. The four named stacks

| Stack (as named) | What it means | Free-tier reality | In ARGUS |
|---|---|---|---|
| **Behavioral Layer** (CryptoQuant + Arkham + Santiment + Glassnode: who is moving coins) | Exchange/miner flows, entity-labelled whales, holder cohorts, social+dev | All four are paid/gated. Free proxies: Lookonchain token feeds, Dune saved queries, Etherscan tracked wallets, DefiLlama stablecoin/bridge flows, Glassnode weekly text, mempool.space | Already the **On-chain factor**. Upgrade path: if Arkham ($19.99) or CryptoQuant ($29) ever fits the budget rule (it doesn't now), they slot into the same factor. Not a separate sleeve. |
| **Derivatives Structure** (CoinGlass + Deribit + Binance/OKX: funding, OI, liquidations, IV) | Positioning and leverage structure | Coinalyze + OKX/Binance/Bybit/Hyperliquid public + Deribit public — fully free | Already the **Technical/positioning factor**. Also the basis of two possible sleeves (§2: funding-arb, squeeze) |
| **Macro Overlay** (FRED + EDGAR + Scira + prediction markets) | Regime, real yields, filings, event odds | Fully free | Already the **Macro factor + regime engine + catalyst factor**. Prediction-market divergence is a candidate sleeve (§2) |
| **Agent-Native** (CoinStats MCP + Scira + CCXT skills + Codex MPP) | Tooling for agents, not a signal | Free tiers exist; MPP (pay-per-call USDC) violates the free rule | **Not a signal stack.** It's plumbing: adopted selectively (`DATA_SOURCES.md` §6b). |

Conclusion: three of the four are already inside the five-factor model — they describe *inputs*, not strategies. The value of naming them is as an audit: each run's dossier shows which stack contributed evidence and which was missing (`data_gaps`).

## 2. Additional sleeves worth designing (crypto, same platform)

| Sleeve | Trigger / thesis | Data (free) | Horizon | Risk profile | Status |
|---|---|---|---|---|---|
| **S1 Five-factor swing** (MVP) | Score ≥ +4 with regime + catalyst | all factors | 1–7d | directional, 1–2% risk | **Building** |
| **S2 Catalyst/event** | Dated unlock, vote, upgrade, ETF decision, earnings of crypto-treasury equities; enter before, exit on event | catalysts.yaml, Tokenomist/CryptoRank, EDGAR, ForexFactory | 1–5d | directional, smaller size, hard time stop | Post-MVP (needs the calendar to be reliable first) |
| **S3 Funding/basis carry** | When funding is extreme, hold the paid side (long spot/short perp or cross-venue) | Coinalyze predicted funding, OKX/Bybit/HL funding | 1–3d | delta-neutral-ish, fee-sensitive | Post-MVP; needs spot or second venue → Bybit KZ |
| **S4 Squeeze / liquidation** | Crowded positioning (L/S ratio, OI/mcap, funding) + price near liquidation cluster → fade or ride the cascade | Coinalyze L/S, liquidations, OKX rubik | hours–2d | high variance; the ZEC lesson: never depend on a single mechanical trigger | Research only; requires intraday runs |
| **S5 Flow** | ETF flows + stablecoin mint/burn + bridge flows as leading indicators for BTC/ETH/chains | Farside, DefiLlama stables/bridges | 3–10d | directional majors | Fold into Macro factor first; sleeve if it shows standalone edge |
| **S6 Narrative/attention** | Attention velocity (X engagement via Scira, Google Trends, CoinGecko category momentum) before price | Scira xsearch metrics, Google Trends (pytrends), CoinGecko categories | 2–7d | directional alts, subjective evidence → capped weight | Research; feeds the satellite screen now |
| **S7 Prediction-market divergence** | Polymarket vs Kalshi vs futures-implied odds diverge → position in the cheaper venue or hedge crypto exposure | Polymarket Gamma, Kalshi, Fed monitor | days–weeks | event bets | **Side project** (Polymarket bot) — advisory `polymarket_watch` in ARGUS only |
| **S8 Options-implied** | DVOL regime, skew, max-pain near expiries; low IV + catalyst → directional; high IV → stand aside | Deribit public | days | modifier | Folded into regime engine; standalone only with options execution (side project) |

Rule for adding any sleeve: it must have (1) a deterministic trigger the gate can test, (2) its own calibration bucket, (3) a risk budget that the meta-allocator can cut to zero, (4) a rule-layer backtest before any live capital.

## 3. Multi-system, multi-market (your broader idea)
"Several trading systems where decisions are made by different triggers/concepts, including different markets (crypto futures, spot, options, stocks)" → the platform/sleeve model above for crypto perps; **separate bot instances** sharing the ARGUS core for other markets (execution, data quirks and risk envelopes differ too much to share one process). Seeds for each are in `SIDE_PROJECTS.md`; the roadmap sequences them after the perp bot has a live track record.
