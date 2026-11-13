# ROADMAP.md — beyond the MVP build plan

`BUILD_PLAN.md` covers Phases 0–6 (design → demo → live-small → auto). This file sequences everything agreed *after* that, with the trigger that unlocks each item. Nothing here starts before Phase 6's gates are met unless marked **parallel**.

## Pre-live fix list — code review 2026-10-07 (before any live key is armed)

Source: `docs/private/CODE_REVIEW_2026-10-07.md` (and `CODE_REVIEW_2026-09-18.md` for the older IDs). All 148
findings it lists as new or still open are below, one row each, generated from the fix agents' reports and checked
by hand. Fixed on 2026-10-07 through PRs #1–#10, #13 and #14, each with a failing regression test first, an independent review
and a fix round; merged with CI green on every head; deployed and smoke-tested under systemd; the real-order demo
tests pass on OKX demo.

**Status:** 138 fixed · 1 partial (D-18: three sub-items need a decision) · 9 open, each needing a decision, owner,
or live venue data — listed first, because none of them is a bug the code can close alone.

### Open — needs a decision (go through `grilling`; numbers move on evidence only)
| ID | What has to be decided |
|---|---|
| X-20 | Drawdown kill: the code enforces 15 % (`DD_HALT_PCT`), `risk_profiles.yaml` says 20 %. One number has to win. |
| D-16 | How long an event keeps the blackout after it *starts* — a new window, so a new [tune] number. |
| D-11 / G-9 | Normalising `funding_8h` by each instrument's real funding interval changes a value the fee model consumes; land together with the gate. |
| G-7 | TP1 distance / remaining plan-shape checks not covered by `plan_problems` (event_thesis and time-stop parts are fixed). |
| G-12 | Leverage always comes out at the step minimum (margin budget ≥ capped notional), so `max_lev` and gate 14 never bind — intended or not? |
| J-8 | Protection state machine: model observed facts (a stop vanishing → TRAILING→NONE) or only intended transitions? |
| D-18a, h, i | Three data-quality sub-items that each need a threshold or a source choice. |
| L-8 | Gate numbers are named in the Trader / Risk Debate / Post-mortem prompts, so agents pre-filter (F-115 lesson). Needs prompt edits **plus** a replay measurement — the Trader currently declines plans the gate would have judged. |

### Open — needs owner or the live venue
| ID | What is needed |
|---|---|
| O-5 | **Deferred by owner (2026-10-07):** no healthchecks.io for now. Revisit after **3 months of live trading**, then decide. Until then nothing alerts if the VPS itself is down; open positions keep their stops at OKX either way. |
| X-7 | A real OKX position row to confirm the `mgnRatio` unit against the 200 % floor (the next real fill provides it). |
| P-6 (off-box) | Where the nightly backup is copied off the box (rclone remote + credentials). Integrity check and `Persistent=true` are done. |
| P-N11 (LICENSE) | Repo is private — no licence needed while it stays private; decide only if it is published (Roadmap Phase 10). |
| P-N2 (follow-up) | `MODE=approve` behaves exactly like `auto` and now says so everywhere. A real approval step needs Telegram — Roadmap Phase 7. |

### Follow-ups found after the review round (2026-10-08)
| Item | Why it matters | Next step |
|---|---|---|
| Consensus lead model fails its schema (load test 2026-10-11) | `mistral/open-mistral-nemo` failed validation on 6 of 10 attempts; one consensus call failed on all 4 models after 105 s, which drops the plan (fail-safe, but a lost trade) | Reorder the consensus chain on `argus eval-agents --agent consensus` evidence, before/after; or the Jev path in `docs/private/JEV_RESEARCH_2026-10-11.md` |
| Groq limits are per direction (load test 2026-10-11) | 429 bodies show 7,000 input and 1,000 **output** tokens/min for `qwen3.8-27b`, not one 8,000 TPM; Groq led only 5 of 26 reviews | Model ITPM/OTPM in `config/models.yaml` and the router's skip; stop leading reasoning roles with Groq |
| Token estimate undercounts ~30% (load test 2026-10-11) | chars/4 gave 8,442 for a prompt Gemini counted at 11,996, so the too-large skip can send prompts that 429 | Calibrate `approx_tokens` with a measured factor and a test |
| Lessons are shared across demo and live | A lesson from demo trades would steer live trading | owner's decision — `docs/private/DECISIONS_2026-10-07.md` Q12 |
| Un-judged trades reach the Reflector with blank outcome | A trade with no post-mortem (closed before 2026-09-24, under HALT or a model outage) arrives with `outcome_r: null`; a lesson can cite a trade whose result the model cannot see | Fall back to the snapshot outcome, or tell the prompt that null means "not judged" (prompt change needs a replay) |
| Fast stop-outs are invisible | A position opened and stopped inside one 20-min watchdog interval never gets an OPEN snapshot, so it gets no post-mortem and the Reflector cannot learn "stop too tight" | Record the entry fill as an OPEN snapshot at placement, or reconcile fills from the venue |
| `outcome_r` uses the last snapshot's mark, not the fill | A stop hit between ticks understates the loss | Use the venue's realised PnL (already fetched for the close row) |
| Trader: both Gemini models failed schema validation (2026-10-08 00:32, 1 run in 4) | That run made no decision | Watch; if it recurs, check output truncation at `max_tokens: 2000` on a 15-asset answer |
| `test_trades.py` misses one cross-account case | A live close while a demo position on the same asset is open; the full suite still catches it | Add the test |
| `plan_behind` / `_episode` live in `runs/review.py` but are journal reads | Lazy import from `journal/trades.py`, ~1.8 s first call (weekly reflect only) | Move under `argus/journal/` |
| Autodeploy's page throttle lives in `/var/lib/argus` (`autodeploy.alerted`) | On a full or read-only disk it cannot record that it paged, so a persistent fault pages every tick (the exit-1 alert is the only noise it makes) | Move it to a `RuntimeDirectory=` if it ever happens |

### Second review (2026-10-10, `docs/muse/`, local) — triage and status
Fixed and merged 2026-10-10 (PRs #22, #23, #25; each failing-test-first, independently reviewed, merged one at a time with the full gate on every merge result; deployed `4542639`; 6/6 real-order demo tests pass):
| Finding | Status |
|---|---|
| P0-1 kill switch ignored `funding_flip` / `event` | **Fixed** — a fired rule blocks new entries on its own position's side, every rule kind (DECISION_LOGIC gate 11) |
| P0-2 unknown margin ratio treated as healthy | **Fixed** — unknown with positions open blocks new entries (never closes); verified on OKX demo that isolated positions report `mgnRatio`, so normal entries are not blocked |
| P0-3 fill before OPEN left the order NEW | **Fixed** |
| P0-4 Telegram `/halt` `/resume` `/close` advertised but not wired | **Fixed in docs/help** — the working controls are `argus halt / resume / flatten`; wiring is Q13 |
| P0-5 library caller skipped settings validation | **Fixed** |
| P1-1 NaN disarmed gate 14 and the sizing ATR band | **Fixed** (non-finite, negative and zero ATR fail closed) |
| P1-2 stale data marked ok | **Fixed** for OKX tickers and CoinGecko; Coinalyze, Hyperliquid and candle series have no venue timestamp or no defined max age — open (design) |
| P1-3 booleans accepted as numbers | **Fixed**; typed envelope payloads = Q15 |
| P1-4 impossible TradingHistory / zero equity passed gate 6 | **Fixed** |
| P1-5 raw adapter errors; fake vs OKX symbol spelling | **Fixed**; algo-order fetch deliberately still fails as a whole (a partial stop list would be unsafe) |
| P1-6 empty-ledger audit; UI migrating at startup | **Fixed**; state-machine enforcement = Q9 |
| P2 quota burned on refused calls; fee interval div-by-zero; unbounded LLM text | **Fixed** |
| P3 tune surface, mypy scope, dead network marker | Not bugs — mypy scope is Q14; [tune] numbers move on evidence only |
Found by the log check in the same round: real-venue demo tests wrote into the production journal (F-154) — **fixed**, PR #24.

### Every finding
| ID | From | Status | PR | Note |
|---|---|---|---|---|
| S-1 | 09-18 | Fixed | #5 |  |
| S-3 | 09-18 | Fixed | #5 |  |
| S-5 | 09-18 | Fixed | #14 | `doctor` fails on misspelt ARGUS keys in .env; validation errors never echo input |
| X-1 | 09-18 | Fixed | #3 |  |
| X-3 | 09-18 | Fixed | #5 |  |
| X-6 | 09-18 | Fixed | #5 |  |
| X-7 | 09-18 | Open — needs venue data | — | Checking the mgnRatio unit against MARGIN_RATIO_FLOOR=200 needs a real OKX position row. The mapping is in position_row(), which belongs to the review |
| X-8 | 09-18 | Fixed | #5 |  |
| X-9 | 09-18 | Fixed | #5 |  |
| X-10 | 09-18 | Fixed | #5 |  |
| X-11 | 09-18 | Fixed | #5 |  |
| X-12 | 09-18 | Fixed | #3 + #14 | sCode + venue side (#3); every closing-side stop moved or none, algoClOrdId on every algo (#14) |
| X-13 | 09-18 | Fixed | #5 |  |
| X-14 | 09-18 | Fixed | #5 |  |
| X-15 | 09-18 | Fixed | #5 |  |
| X-16 | 09-18 | Fixed | #5 |  |
| X-17 | 09-18 | Fixed | #5 |  |
| X-18 | 09-18 | Fixed | #5 |  |
| X-19 | 09-18 | Fixed | #14 | one half-open probe (router peeks without consuming it); one source for the kill-switch numbers |
| X-20 | 09-18 | Open — decision | — | DD_HALT_PCT=15 vs risk_profiles.yaml's 20 means choosing one [tune] number through the grilling gate. Gate numbers must not be changed here. |
| G-1 | 09-18 | Fixed | #7 |  |
| G-2 | 09-18 | Fixed | #7 |  |
| G-3 | 09-18 | Fixed | #7 |  |
| G-4 | 09-18 | Fixed | #7 |  |
| G-5 | 09-18 | Fixed | #7 |  |
| G-6 | 09-18 | Fixed | #7 |  |
| G-7 | 09-18 | Open — decision | — | Partly fixed. The event_thesis part is closed under G-N6/L-2 (8707efd), and time_stop_hours is now bounded 1..168 by plan_problems (G-6). Still open:  |
| G-8 | 09-18 | Fixed | #7 |  |
| G-9 | 09-18 | Fixed | #7 |  |
| G-10 | 09-18 | Fixed | #7 |  |
| G-11 | 09-18 | Fixed | #7 |  |
| G-12 | 09-18 | Open — decision | — | Leverage is always the step minimum, because margin_budget = equity x max_notional_frac is always >= the capped notional. max_lev, spec.max_leverage a |
| G-13 | 09-18 | Fixed | #7 |  |
| G-14 | 09-18 | Fixed | #7 |  |
| G-15 | 09-18 | Fixed | #7 |  |
| G-16 | 09-18 | Fixed | #7 |  |
| G-17 | 09-18 | Fixed | #7 |  |
| G-18 | 09-18 | Fixed | #7 |  |
| G-19 | 09-18 | Fixed | #7 |  |
| G-20 | 09-18 | Fixed | #7 |  |
| J-1 | 09-18 | Fixed | #4 |  |
| J-2 | 09-18 | Fixed | #4 |  |
| J-3 | 09-18 | Fixed | #4 |  |
| J-4 | 09-18 | Fixed | #4 |  |
| J-5 | 09-18 | Fixed | #4 |  |
| J-6 | 09-18 | Fixed | #4 |  |
| J-7 | 09-18 | Fixed | #4 |  |
| J-8 | 09-18 | Open — decision | — | Needs a decision. tests/journal/test_states.py deliberately asserts TRAILING->NONE is illegal; whether the protection machine models observed facts (a |
| J-9 | 09-18 | Fixed | #4 |  |
| J-10 | 09-18 | Fixed | #4 |  |
| J-11 | 09-18 | Fixed | #4 |  |
| J-12 | 09-18 | Fixed | #4 | repeated `--symbols` deduplicated |
| O-1 | 09-18 | Fixed | #4 |  |
| O-2 | 09-18 | Fixed | #4 |  |
| O-3 | 09-18 | Fixed | #4 |  |
| O-4 | 09-18 | Fixed | #4 |  |
| O-5 | 09-18 | Open — needs owner | — | Per-unit healthchecks need owner to create one healthchecks.io check per unit (manager, review, reflect) and add each URL to .env, plus config fields. Th |
| P-1 | 09-18 | Fixed | #8 |  |
| P-2 | 09-18 | Fixed | #8 |  |
| P-5 | 09-18 | Fixed | #8 |  |
| P-6 | 09-18 | Fixed | #8 |  |
| P-7 | 09-18 | Fixed | #8 |  |
| P-8 | 09-18 | Fixed | this PR | STATUS header and BUILD_PLAN reconciled by the coordinator |
| P-9 | 09-18 | Fixed | #8 |  |
| P-10 | 09-18 | Fixed | #8 |  |
| P-11 | 09-18 | Fixed | #8 |  |
| P-12 | 09-18 | Fixed | #8 |  |
| P-14 | 09-18 | Fixed | #1 |  |
| P-15 | 09-18 | Fixed | #8 |  |
| P-16 | 09-18 | Fixed | #8 |  |
| D-1 | 09-18 | Fixed | #7 |  |
| D-2 | 09-18 | Fixed | #13 | cross-venue price consensus (OKX, Bybit KZ, Binance, Hyperliquid) computed live; unknown or divergent ⇒ gate 2 REJECT; missing venues named in the log |
| D-3 | 09-18 | Fixed | #7 |  |
| D-4 | 09-18 | Fixed | #6 |  |
| D-5 | 09-18 | Fixed | #6 |  |
| D-7 | 09-18 | Fixed | #6 |  |
| D-8 | 09-18 | Fixed | #6 |  |
| D-9 | 09-18 | Fixed | #6 |  |
| D-10 | 09-18 | Fixed | #7 |  |
| D-11 | 09-18 | Open — decision | — | Same root as G-9: normalising funding_8h by the real funding interval changes a value the gate's fee model consumes, so it must land together with the |
| D-12 | 09-18 | Fixed | #6 |  |
| D-13 | 09-18 | Fixed | #6 |  |
| D-14 | 09-18 | Fixed | #6 |  |
| D-15 | 09-18 | Fixed | #6 |  |
| D-16 | 09-18 | Open — decision | — | Keeping an event in the blackout N hours after it starts needs a new post-start window - a [tune] number plus a DECISION_LOGIC.md update; numbers may  |
| D-17 | 09-18 | Fixed | #6 |  |
| D-18 | 09-18 | Partial | #6 | D-18b–g, j fixed; D-18a, h, i need a design decision (see below) |
| R-1 | new | Fixed | #3 |  |
| G-N1 | new | Fixed | #7 |  |
| G-N2 | new | Fixed | #7 |  |
| G-N3 | new | Fixed | #7 |  |
| G-N4 | new | Fixed | #7 |  |
| G-N5 | new | Fixed | #7 |  |
| G-N6 | new | Fixed | #7 |  |
| G-N7 | new | Fixed | #7 |  |
| L-2 | new | Fixed | #7 |  |
| L-3 | new | Fixed | #3 |  |
| L-4 | new | Fixed | #2 |  |
| L-5 | new | Fixed | #7 |  |
| L-6 | new | Fixed | #2 |  |
| L-7 | new | Fixed | #2 |  |
| L-8 | new | Open — decision | — | Thresholds named in prompts (trader.md:34-71, trader_batch.md:51-88, riskdebate.md:49-50, postmortem.md:36-39) need prompt edits plus replay measureme |
| L-9 | new | Fixed | #7 |  |
| L-10 | new | Fixed | #2 |  |
| L-11 | new | Fixed | #2 |  |
| L-12 | new | Fixed | #3 |  |
| L-13 | new | Fixed | #2 |  |
| L-14 | new | Fixed | #3 |  |
| X-N2 | new | Fixed | #5 |  |
| X-N3 | new | Fixed | #5 |  |
| X-N4 | new | Fixed | #5 |  |
| X-N5 | new | Fixed | #5 |  |
| X-N6 | new | Fixed | #5 |  |
| X-N7 | new | Fixed | #5 |  |
| X-N8 | new | Fixed | #5 |  |
| X-N9 | new | Fixed | #5 |  |
| X-N10 | new | Fixed | #8 |  |
| X-N11 | new | Fixed | #5 |  |
| D-N1 | new | Fixed | #3 |  |
| D-N2 | new | Fixed | #7 |  |
| D-N3 | new | Fixed | #7 |  |
| D-N4 | new | Fixed | #6 |  |
| D-N5 | new | Fixed | #6 |  |
| D-N6 | new | Fixed | #6 |  |
| J-N1 | new | Fixed | #4 |  |
| J-N2 | new | Fixed | #3 |  |
| J-N3 | new | Fixed | #3 |  |
| J-N4 | new | Fixed | #4 |  |
| J-N5 | new | Fixed | #3 |  |
| J-N6 | new | Fixed | #4 |  |
| J-N7 | new | Fixed | #4 |  |
| J-N8 | new | Fixed | #4 |  |
| J-N9 | new | Fixed | #4 |  |
| J-N10 | new | Fixed | #3 |  |
| J-N11 | new | Fixed | #3 |  |
| J-N12 | new | Fixed | #8 |  |
| J-N13 | new | Fixed | #8 |  |
| P-N1 | new | Fixed | #8 |  |
| P-N2 | new | Fixed | #8 |  |
| P-N3 | new | Fixed | #8 |  |
| P-N4 | new | Fixed | #8 |  |
| P-N5 | new | Fixed | #8 |  |
| P-N6 | new | Fixed | #8 |  |
| P-N7 | new | Fixed | #8 |  |
| P-N8 | new | Fixed | #8 |  |
| P-N9 | new | Fixed | #4 |  |
| P-N10 | new | Fixed | #1 |  |
| P-N11 | new | Fixed | #8 |  |

## Agent-quality metrics — proposed 2026-11-02 (decisions M1–M7 in `docs/private/DECISIONS_2026-11-02.md`)

Online metrics: computed continuously from the journal on real forward runs (demo now, live later), not
from the offline golden suites. Most need **one shared piece first: a shadow-outcome engine** —
for *every* plan the pipeline saw (traded, vetoed by an agent, or refused by the gate), walk the
OKX candles forward and record which came first: stop, TP1 or the time stop, plus the R it would
have made. Without it, veto precision and calibration wait months for real fills (2 AI trades in
two weeks); with it, every run adds ~5–15 labelled rows. `journal/shadow.py` and
`journal/calibration.py` are already on the plan as "planned" (J12).

| # | Metric | How (proposed) | Target | Exists today? | Priority |
|---|---|---|---|---|---|
| M1 | Shadow outcome per plan | candles after the decision → stop / TP1 / time-stop first, R | — (enabler) | no | **1st** |
| M2 | Veto precision | per blocker (consensus, red team, risk debate, each gate rule): mean shadow R of what it blocked vs what passed | blocked mean R < passed mean R | no (needs M1) | **2nd** |
| M3 | Calibration | win rate and mean R by Trader score bucket (traded + shadow) | rises with the score | no (J12/J13) | 3rd |
| M4 | Invented-evidence rate | `vettings.unsupported` / Trader plans (code check `llm/evidence.py` already blocks them) | trend down; < 10% | data yes, no view | quick |
| M5 | Missing-verdict rate | `vettings.checks` entries "unavailable" / plans | < 5% | data yes, no view | quick |
| M6 | Consensus convergence | AGREE / plans where consensus ran (report ONE_SIDED and CONFLICT too) | 40–80% | data yes, no view | quick |
| M7 | Judge score per agent | weekly sample of N decisions per role, LLM judge with a rubric, 1–5 | avg ≥ 4 | no (postmortem verdict + golden suites only) | later |

**Reviewed against the proposal:**
- **Veto precision as written ("vetoed plans that would have hit their stop > 50%") does not prove
  value.** With RR(TP1) ≥ 1.5 a plan with no edge hits its stop first ~60% of the time, so > 50% is the
  base rate. Compare the blocked set with the passed set (mean R, stop-first rate) instead, per blocker.
  Apply it to the **gate** too: gate 3 refused most plans in Oct–Nov, and that is the rule we most
  need evidence for before tuning (CLAUDE.md: [tune] numbers move on evidence only).
- **Invented-evidence target "0"** can only hold for plans *reaching the gate* (code already guarantees
  it). For Trader *proposals* it is a model-quality trend: ~4 of ~25 plans were blocked by evidence
  between 10-29 and 11-01.
- **Consensus convergence** measured 10-20→10-29: ~6 AGREE in ~22 (27%), ONE_SIDED 10, CONFLICT 4,
  DIVERGENT 1. Below 40% mostly because Consensus says "no trade"; report all four shares.
- **Judge score** is the only metric that costs LLM calls every week and the hardest to trust (a judge
  model grading a sibling model). Do it last, sampled, on a different model family than the judged role.
- **Sharpe ratio, $ and % PnL** (today / 7d / 30d / all) belong to the PnL panel being added to the
  dashboard, not to this table: they measure the account, these measure the agents.
- **Telegram alerts** (403 since October): parked as low priority (owner, 2026-11-02).

## Beyond the MVP

| Phase | Item | Trigger / why then | Source of the idea |
|---|---|---|---|
| **7** | Approve mode (`QUEUED` plans, `/approve`, dashboard buttons) | You are 99% auto; build after MVP but keep the switch | Q3 |
| 7 | Bybit KZ execution fallback (adapter behind `ExchangeAdapter`; data already in Phase 2) | OKX circuit breaker open twice in a month, or geo issues | your note |
| 7 | Journal MCP (read-only tools: `why`, `calibration`, `search_lessons`) | Schema stable after 30 live trades | your note |
| 7 | Scout agent (weekly universe proposals with evidence; you confirm) | Core/whitelist rules proven in demo | `UNIVERSE_OPTIONS.md` |
| 7 | Guardrails AI wrappers for validators (shared with stocks bot) | When the stocks instance starts | typed validators |
| **8** | Event-driven wake-up: OKX price/funding alerts → webhook → ad-hoc Review; inference gating (z-score anomaly → extra Manager run) | Watchdog shows ≥3 cases/month where a 6h wait cost >1R | Scira v1 §6.4, v2 §4.2 |
| 8 | Local finance model on Oracle free ARM (Fin-R1 7B / finance LoRA) as third consensus family + post-mortem writer; re-evaluate Trading-R1 if weights ship | Free-tier quota pressure or a measured benefit in model-eval | `LLM_PROVIDERS_AND_MODELS.md` §4 |
| 8 | Paid Trader/Reviewer model (DeepSeek → Claude/GPT/Gemini Pro) | Calibration shows the free tier is the bottleneck, not the data | Q, budget rule |
| 8 | Meta-allocator across sleeves; sleeves S2 (catalyst) and S3 (funding carry) | S1 has 60+ live trades and PF ≥ 1.3; Bybit adapter exists for S3 | `STRATEGY_STACKS.md` |
| **9** | Embeddings alongside FTS; asset-memory summaries; Postgres migration | >2k trades, second bot instance, or hosted MCP | `MEMORY_AND_STATE.md` |
| 9 | Factor attribution (per-factor IC-style stats; Shapley only if ≥200 trades) | Enough trades to be non-noise | Scira v2 §4.3 (reduced) |
| 9 | Sleeves S4/S5/S6 research (squeeze, flow, narrative) via rule-layer backtests | Data history accumulated in the journal | `STRATEGY_STACKS.md` |
| 9 | LoRA fine-tune of our own Analyst on the journal | ≥1k stored analyses with outcomes | why we store everything |
| **10** | Side projects start (`SIDE_PROJECTS.md`): stocks instance first | Perp bot has a 3-month live track record | Q12 |
| 10 | Public read-only track-record page; SaaS packaging decision | Track record + demand | your original note |

**Parallel, any time:** free-provider onboarding (`LLM_PROVIDERS_AND_MODELS.md` §1); `catalysts.yaml`, `tokenomics.yaml`, `whitelist.yaml` upkeep; FAILURE_LOG entries; monthly source-tier re-verification.

**Explicitly not planned:** HFT/market-making, CoinStats/Codex pay-per-call data, Kelly-from-confidence sizing, LangGraph, vector DB in the core loop, NeMo Guardrails, free public VPNs for API traffic.
