# BUILD_PLAN.md — Phases, milestones, gates

Principle: **protection and journal before intelligence.** A bot that can't guarantee an exchange-side stop and can't replay its own decisions must not trade. LLM alpha is layered on last.

Effort assumes you + Claude Code, ~2–3 focused evenings/week. Durations are elapsed, not effort.

---

## Phase 0 — Decisions & environment (2–3 days)
- [x] Q1–Q14 answered 2026-09-14; N1–N7 answered 2026-09-15 (`OPEN_QUESTIONS.md`). No decisions open.
- [x] Repo skeleton generated 2026-09-15 (`argus-project/`: package layout, config, knowledge seeds, sizing module + tests, doctor CLI, systemd templates).
- [ ] Onboard free LLM providers: NVIDIA NIM, Cloudflare Workers AI, GitHub Models, Zhipu, SambaNova (+ Cerebras if still free); DeepSeek prepaid $10 as the only paid fallback; Logfire personal project (`LLM_PROVIDERS_AND_MODELS.md`, `OBSERVABILITY_AND_GUARDRAILS.md`).
- [ ] Repo scaffold (`uv`, `argus/` package, `pyproject`, `pre-commit`, `pytest`, `ruff`, `mypy`, Pydantic AI, Logfire), `.env.example`, `knowledge/` seeded from neighbor chat (`sources.md`, `catalysts.yaml`, `tokenomics.yaml`, `whitelist.yaml`, `blocklist.yaml`), root `AGENTS.md` + `CLAUDE.md`.
- [ ] OKX **demo** API key; confirm `x-simulated-trading` works from your laptop via ccxt (`set_sandbox_mode(True)`).
- [ ] Provision a suitable VPS; confirm OKX/Binance/Gemini/Groq reachable from it; note static IP.
- **Gate:** `argus doctor` prints green for exchange (OKX demo), Bybit KZ public, ≥6 LLM providers, Coinalyze, CoinGecko, DefiLlama, FRED, CryptoPanic, Scira, `sec-edgar-mcp`, Logfire.

## Phase 1 — Exchange adapter, journal, watchdog (week 1–2)
- [ ] `ExchangeAdapter` (ccxt/OKX): balances, positions, OHLCV, funding/OI, `place_bracket` with `attachAlgoOrds` (SL mark-trigger + TP1), `amend_stop`, `place_trailing`, `partial_close`, `close`, `cancel_all`, `reconcile`. Net mode + isolated enforced.
- [ ] SQLite journal (WAL), migrations, `runs/orders/fills/positions_snapshots/position_events/kill_switches` tables; **state machines** with terminal states + TTLs; hash-chained ledger tables; atomic transitions.
- [ ] **Watchdog**: every 20 min → reconcile, verify SL exists per position, margin ratio, DD kill-switch, HALT file, Telegram alerts, healthchecks ping.
- [ ] Telegram bot: `/status /positions /halt /resume /close`.
- [ ] Resilience: per-dependency circuit breakers, backoff+jitter counting attempts, exchange-response validation (zero-qty fills), fee rates from account endpoint + reconciliation from fills, per-instrument precision via ccxt, pending-order TTL + 2% drift cancel.
- [ ] Unit tests: attachAlgoOrds payloads (SL/TP/**trailing**), idempotent clOrdId, reconcile diffs, precision per instrument, state-machine transitions (every state reaches a terminal), circuit breaker, fee reconciliation.
- **Gate:** on demo, manually open a position via API *without* SL → watchdog repairs it within 20 min and alerts. Kill `/halt` stops all runs.

## Phase 2 — Data layer & quality checks (week 2–3)
- [ ] Fetchers with `Observation` envelope + cache + rate limits: OKX public (+rubik), **Bybit KZ** public, Binance futures public, Coinalyze, Hyperliquid, Deribit, CoinGecko, CMC, F&G, DefiLlama (TVL/fees/stables/emissions/**bridges**), FRED, ForexFactory calendar, Polymarket, **Kalshi**, Farside (scrape), CryptoPanic, Google News RSS, Jina Reader, **`sec-edgar-mcp`** (raw `efts` fallback), mempool.space, Scira (search/xsearch/reddit), CoinStats MCP (evaluation only).
- [ ] Data-quality module: one function + one test per trap (18); `data_quality_events` table; price consensus.
- [x] Regime snapshot + universe (core 12 + satellites ≤3 + whitelist) + dossier builder with `evidence_class` tags + asset memory cards (deterministic). *(2026-09-17)*
- [x] `argus dossier BTC` CLI prints a dossier with provenance; `argus regime` prints the regime. *(2026-09-17)*
- **Gate:** 3 consecutive days of dossiers for BTC/ETH/SOL/ARB with all trade-critical families present, and every trap test green.

## Phase 3 — Agents, gate, Manager run in shadow mode (week 3–4)
- [ ] LLM router with quotas + JSON validation + repair + fallbacks; prompts v1 (`argus/prompts/`). *(waiting on LLM keys)*
- [ ] Regime Analyst, Factor Analyst (batched), Red Team, **Risk Debaters**, Trader, **Consensus Trader (on from day one; `consensus_events`)**, Catalyst Extractor; **Risk Gate (all 14 gates incl. balance/min-size, fee viability, downside-miss) — done 2026-09-17**; sizing math + **small-account steps** + tests (worked example must reproduce `DECISION_LOGIC.md` §5).
- [ ] Model matrix + `argus eval-models` weekly golden run across all providers (`LLM_PROVIDERS_AND_MODELS.md` §2); Logfire spans on every stage.
- [x] Manager run end-to-end in **shadow** (no orders): plans + gate results + shadow ledger only. *(2026-09-18; LLM agents still stubbed)*
- [ ] Golden dossiers (20) + schema tests + adversarial injection tests.
- [ ] `argus replay <run_id>` offline re-run.
- **Gate:** 7 days of shadow Manager runs, ≥10 gated plans, zero schema failures unhandled, `/why` works, LLM calls/day ≤ 60 with ≥3 providers healthy, consensus disagreement rate reported.

## Phase 4 — Reviewer + execution on OKX demo (week 5–8, ≥4 weeks of demo)
- [ ] Review run: reconcile → deterministic checks → LLM Reviewer → review gate → execute (partial close, move SL, trail, close); **default trailing stop enforced on every position**; Post-mortem per closed trade; closed-position Telegram events; `/pnl`, `/portfolio`.
- [ ] **Dashboard MVP** (`UI_DESIGN.md` screens 1–5, 8) served via SSH tunnel; screens 6, 7, 9 by end of Phase 4.
- [ ] Manager run places bracket orders on demo; unfilled-order expiry; TP1→BE logic; time stops; plan kill-switches.
- [ ] Rule-layer backtest (`argus backtest-rules`) over 2 years OKX/Binance history: verify exit/sizing rules are not negative-expectancy under random entries; tune ATR bands/trail callback **on backtest only**.
- [ ] Reflector weekly + lessons promotion; calibration report in `/weekly`.
- [ ] `FAILURE_LOG.md` filled from demo incidents; `STATUS.md` weekly.
- **Gate (go-live):** ≥50 shadow decisions and ≥15 closed demo trades; **zero** watchdog "missing SL/trail" repairs in the last 14 days; no unhandled exceptions in 14 days; calibration buckets not inverted (higher score ≥ lower score win rate, n permitting); fee+funding drag measured; consensus veto stats reviewed; you have read every `/why` for the last 10 trades and agree with ≥7.

## Phase 5 — Live-small in `auto` mode (week 9–12)
- [ ] Live trade-only key on a **sub-account** funded with **$100–200** (Q1), starting flat; IP-bound to VPS; `LIVE_TRADING` interlock.
- [ ] Mode `auto` (your choice) with **small-account mode** steps enabled (`DECISION_LOGIC.md` §5.1), ≤2 open positions while equity < $500; Telegram closed-position feed + dashboard reviewed daily.
- [ ] Weekly review of calibration, drag, and lessons; `STATUS.md` updates.
- **Gate (to scale capital toward $1k):** 30 closed live trades, PF ≥ 1.3, max DD ≤ 12% (≤ 20% while in small-account step ≥ 2), fee drag < 25%, ≥ 80% of proposed plans you would have approved anyway.

## Phase 6 — Autonomous + scale (month 4+)
- [ ] Capital to ~$1k; exit small-account mode; risk_per_trade → 1.5–2% only per calibration rule; approve mode implemented as a switch (Roadmap Phase 7).
- [ ] Capital additions in steps ≤ 50% per month, each step after a clean 4-week window.
- [ ] Post-MVP features by measured need: consensus mode on by default if it reduces losing-bucket frequency; Binance/Bybit data adapter; event-driven wake-up; Scira Pro if X-search evidence appears in ≥30% of winning theses; stocks instance.

---

## Definition of done (MVP)
- Every position has a live exchange-side SL 100% of the time (watchdog-verified).
- Every decision is replayable from stored inputs.
- ≤ €10/month all-in (ceiling €15; only VPS + paid-LLM fallback are paid).
- Calibration and expectancy reported weekly without manual work.
- You can explain any trade in one `/why`.

## Explicitly deferred
Vector DB · LangGraph · Prometheus/Grafana (Logfire instead) · multi-exchange execution (Bybit KZ data only) · approve mode (Phase 7) · options data beyond DVOL/put-call · paid on-chain APIs · sub-hourly reactions · strategy sleeves beyond S1 (`STRATEGY_STACKS.md`).
