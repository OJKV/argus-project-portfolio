# UNIVERSE_OPTIONS.md — which assets does ARGUS research and trade?

**DECIDED 2026-09-15: Option D, core-12 as listed, satellites ≤3, whitelist expiry 30d.** Encoded in `knowledge/universe.yaml`, `knowledge/whitelist.yaml`, `knowledge/blocklist.yaml`.

Original analysis (Q8 / your note). Four options, scored on the things that actually differ. Costs are not the deciding factor: dossier building is code (cheap), and the LLM analyses 3–4 assets per call, so 12 vs 20 assets is 3 vs 5 calls.

## The options

| | A · Fixed core (top 10–20 by mcap) | B · Dynamic screen (v0.9 §5.2) | C · "Hot coins" (CMC trending/gainers — prototype) | D · **Core + Satellites + Whitelist** (recommended) |
|---|---|---|---|---|
| What is researched each run | same 10–20 assets | top-8 by attention score + BTC/ETH/SOL | top-30 gainers/volume from CMC | 12 core (always) + ≤3 satellites from the screen + your whitelist |
| Research depth per asset | **high** — asset memory accumulates; theses compare to the asset's own history | medium — an asset may appear once and vanish | low — different micro-caps daily | high for core, medium for satellites |
| Alpha opportunity | lower — majors are the most efficiently priced; moves are BTC-beta | higher — catches rotations, unlock shorts, narrative pumps (ARB/ZEC/DASH/TAO in Sep were all screen finds) | highest *apparent*, lowest *realised* — you arrive after the move; slippage, fees, no perp listing | core catches regime trades; satellites catch rotations |
| Data quality | **best** — every core asset has T1 data across all five factors | mixed — small caps have `data_gaps` in fundamentals/on-chain | worst — gaps everywhere, slug drift, subscript-zero traps | best for core; satellites gated by data-quality gate |
| Liquidity / execution | best (tight spreads, deep books, OKX max leverage) | good (vol ≥ $30M filter) | poor | good |
| Correlation | worst — top-10 are one BTC-beta trade; 4 positions = 4× the same bet | better — mixed betas | random | managed by heat gate (≤2 same-direction high-beta) |
| Calibration learning | fastest — repeated assets, per-asset stats | slowest — samples spread thin | meaningless | fast for core |
| Regime/rotation adaptivity | none (manual) | automatic | automatic but noisy | automatic via satellites + scout agent |
| Maintenance | slug map for 12 assets, stable | slug map for ~100 OKX perps, monthly checks | constant breakage | 12 stable + ~100 checked monthly |
| LLM/API calls per Manager run | 3–5 analyst calls | 3 analyst calls | 8+ | **4 analyst calls** |
| Matches the Sep research runs? | no — those runs were mostly alts | yes | no (the runs explicitly stopped using CMC gainers) | yes, plus the majors those runs always covered |

## Why not pure A
Top-10 by market cap is one trade in ten costumes: in a risk-off week every long loses together. The neighbor runs' best-scored setups (ARB unlock short +8, DASH short +6) were outside the top 20. And "research more deeply" only pays if the asset has idiosyncratic drivers — BTC's drivers are macro/flows (covered by the regime layer), not per-asset research.

## Why not pure B (my v0.9 default)
It has no memory: an asset appears, gets traded, disappears; lessons don't compound. Small caps bring data gaps, and the screen can be gamed by pumps (relative volume spikes on nothing).

## Why not C
The Sep runs documented it: CoinGecko/CMC gainers are hard-scoped to top-1000, dominated by micro-caps, omit real large-cap movers, and most of those coins have no OKX perp or no leverage. It optimises for "what already moved".

## Recommendation: D — Core + Satellites + Whitelist, with a Scout agent later

```
core (12, researched every Manager run; edited only by Scout proposal + your confirmation):
  BTC, ETH, SOL, XRP, ZEC, DOGE, HYPE, ARB, UNI, SUI, BNB, LINK     ← re-cut 2026-09-16 on measured OKX liquidity
  (dropped: GRAM/ex-TON $3.5M/24h rank 138 · AVAX $21.4M rank 48 · NEAR $39.6M rank 37 — all below the $50M core floor)
satellites (≤3 per run): attention-score top-3 from the OKX screen (vol ≥ $30M, age ≥ 90d) not already in core
whitelist (`knowledge/whitelist.yaml`): your manual adds (ZEC, ARB, DASH, TAO …) — researched every run while listed, expire after 30 days unless renewed
blocklist: memecoins with FDV < $300M, anything with < 90 days of OKX perp history
cap: ≤ 15 dossiers per run (4 analyst calls); satellites yield to whitelist if over cap
```
- **Asset memory** (`ARCHITECTURE.md` §11) makes the core research compound: typical ATR%, funding regime, unlock cadence, past theses + outcomes are in every core prompt.
- **Scout agent (post-MVP, weekly, 1 call):** proposes core/whitelist additions and removals with evidence (listing age, volume trend, data availability, recent attention); you confirm in Telegram. This is the "additional agent which reviews and adds new tickers" from your note.
- **Cost:** ≈ 4 analyst calls per run — the same as B, 1 more than A. The cost argument for "shortlist hot pairs first, then targeted search" is already built in: the *screen* is code and free; deep fetches (Scira X/Reddit, article bodies) happen only for the top-3 satellites and any core asset with a catalyst inside 7 days.

## Decision
Confirmed by owner 2026-09-15: D as written. **Amended by owner 2026-09-16: core membership is liquidity-first.** Core floor (`knowledge/universe.yaml: core_liquidity_floor`): OKX USDT perp 24h turnover ≥ $50M, max leverage ≥ 50x, spread ≤ 0.03%, crypto-native. `argus doctor --universe` measures all four from `/market/tickers` + `/public/instruments` every run of the check; any failure is reported, not silently dropped.

Why liquidity-first: a $100–200 account is fee- and slippage-dominated, and the thin names were also the ones with the least reliable free data. Measured 2026-09-16 (OKX, 24h turnover): ETH $9.7B · BTC $8.5B · ZEC $1.38B · SOL $1.11B · XRP $885M · DOGE $314M · HYPE $233M · ARB $221M · UNI $159M · SUI $83M · BNB $53M · LINK $53M. Minimum notional on every one is ≤ $11.72 (BTC $7.54, ETH $2.38), so all twelve remain tradable under small-account mode.

Watch item: ZEC's $1.38B turnover is a single-day observation during an outsized move; if its 7-day median turnover falls below the floor it leaves core the same way GRAM did. Tokenized-equity perps (SNDK, XAU, MU, SOXL, SPCX) clear the satellite volume screen but must never enter the universe — see `FAILURE_LOG.md` F-038.

---

## Whitelist (decided 2026-09-17) and candidates for later expansion

**Active whitelist** (`knowledge/whitelist.yaml`, each entry expires 30 days after `renewed`): **FIL, ENA, AAVE** (2026-09-17), **PEPE, NEAR, BCH** (added 2026-11-02 on fresh OKX measurements; all three on the demo venue) — owner's picks from the tier list below. `max_dossiers_per_run` raised 15 → 18 so satellites keep their three slots (+1 analyst call per Manager run).

**Floors.** Whitelist entries use the satellite screen (≥ $30M/24h turnover, ≥ 10x, spread ≤ 0.06%, crypto-native); core uses ≥ $50M. Both are measured by `argus doctor --universe` on the **7-day median** of closed daily candles, not a single 24h snapshot — LINK read $53M one day and $47M the next, and a one-day floor would make membership flap on noise.

**Criteria (owner):** liquid, volatile, not a scam, trending character, good community reputation. Numbers below were measured on OKX on 2026-09-16 (24h turnover, 30-day annualised volatility from daily closes, 30-day return, funding per 8h). Re-measure before adding anything — a single month is one regime.

### Crypto — Tier A (strongest candidates)

| Asset | $M/24h | Vol | 30d | Spread | Case | Status |
|---|---|---|---|---|---|---|
| **FIL** | 114 | 110% | +20% | 0.012% | Filecoin; 7 years, real network, full CoinGecko + DefiLlama coverage, flat funding; trends in long clean legs | **whitelisted** |
| **ENA** | 47 | 183% | +69% | 0.007% | Ethena; most volatile credible name, tight spread; USDe yield is mechanically tied to perp funding, so the derivatives family is a fundamental input | **whitelisted** |
| **AAVE** | 38 | 92% | +35% | 0.008% | Blue-chip DeFi with real fees/revenue — the best factor-1 evidence of any candidate. Weak point: liquidity (7d median $29M on 2026-09-17, just under the floor) | **whitelisted** — watch liquidity |
| **PEPE** | 115 | 116% | +29% | 0.030% | Reference memecoin, deepest book after DOGE; a clean read on retail risk appetite. No product — "not a scam" in the sense of "won't rug", not "has fundamentals" | candidate |

### Crypto — Tier B (good, one flaw each)

| Asset | $M/24h | Vol | Flaw |
|---|---|---|---|
| **WLD** | 66 | 106% | Strongest narrative and its unlock schedule suits the unlock-short logic — but the same overhang plus privacy controversy make "good reputation" arguable |
| **ADA** | 58 | 84% | Large community, 6.5 years; widest spread in the set (0.052%) and low volatility for an alt |
| **XLM** | 43 | 83% | Tight, old, real payments use; funding −0.043%/8h at measurement (crowded short) |
| **TAO** | 29 | 88% | Best AI narrative, serious community; liquidity at the floor, spread 0.047% |
| **BCH** | 35 | 116% | Old, respected, volatile — nothing wrong except that it is dull to research |

### Crypto — Tier C (skip unless something changes)

- **TRUMP** ($120M, vol 161%) — liquid and trending, but insider concentration, unlock overhang, pure headline risk.
- **USELESS** (+501% in 30d, vol 311%) — a live mania; the "arrive after the move" failure option C was rejected for.
- **PUMP** ($87M) — pump.fun token; real revenue, poor reputation.
- **LIT, CAP, CNPY, PONS, KORU, ZHIPU, SNXX, BZ** — could not be identified with confidence; several were < 90 days old with 10–25x max leverage (OKX's low-tier listing signature).
- **LTC, DOT, OP, APT, ONDO, CRV, TIA, SEI, JUP, LDO** — sound assets, but under $30M on OKX. Re-check if their OKX depth improves.
- **AVAX ($21M), NEAR ($40M), GRAM/ex-TON ($3.5M)** — dropped from core on 2026-09-16 for liquidity.

### Tokenized assets (OKX lists equity/commodity perps)

The five-factor model assumes on-chain, tokenomics and DefiLlama data exist. For most tokenized assets four of five factors are structurally zero, so they cannot reach the +4 threshold honestly, and `universe.yaml: crypto_native_only` keeps them out of the automatic screen. Two structural costs apply to all of them: **gaps** (the underlying trades 9:30–16:00 ET; the perp trades 24/7) and **funding** of 1.5–3%/month versus ~0 for crypto.

| Asset | $M/24h | Vol | Funding/8h | Verdict |
|---|---|---|---|---|
| **MSTR** (Strategy) | 41 | 94% | 0.026% | **Tier A−.** A BTC proxy with a balance sheet, and the design already plans an EDGAR pipeline for crypto-treasury equities (`DATA_SOURCES.md` §6). The one tokenized name that fits the existing factor model. Needs `crypto_native_only` relaxed for an explicit allowlist first. |
| **CRCL** (Circle) | 47 | 88% | 0.035% | **Tier B.** USDC issuer, so stablecoin supply is a real fundamental. Carry ≈ 3.8%/month — gate 7 (funding) would reject most holds. |
| **XAU / XAG / CL** (gold, silver, crude) | 244 / 44 / 147 | 21 / 34 / 36% | ~0 | Excellent liquidity, zero funding — but volatility is so low that ATR-based stops become sub-1%. Candidate as a *regime hedge* sleeve (`STRATEGY_STACKS.md`), not a swing trade. |
| **SNDK, MU, SOXL, SPCX, INTC** | 29–675 | 48–109% | 0.002–0.019% | Skip — pure equities with no crypto linkage. Belongs to the stocks instance (`SIDE_PROJECTS.md` SP1) if anywhere. |

### Budget reminder

Every whitelist name is one more dossier per Manager run. The analyst batches four dossiers per call, so each fourth addition costs one more LLM call per run (two per day). The hard ceiling is the LLM budget (≤ 60 calls/day) and the Scira quota (20/day, rationed to the top candidates), not the dossier count itself.
