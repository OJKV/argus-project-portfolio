# OBSERVABILITY_AND_GUARDRAILS.md — logging, tracing, LLM guardrails

## 1. Observability

**Goal (yours):** store every input, reasoning and output of the system so it can be analysed and improved. Two stores, one truth:

| Store | What | Why both |
|---|---|---|
| **Journal (SQLite)** — system of record | Every observation envelope, dossier, LLM call (prompt hash + full prompt text + raw output incl. reasoning + parsed result + validation outcome), gate result, order, fill, review, post-mortem, data-quality event, circuit-breaker event | Replayable, auditable, queryable offline, survives vendor changes |
| **Pydantic Logfire** — traces & dashboards | Spans for every run stage, every HTTP call (httpx instrumentation), every Pydantic AI agent call (tokens, latency, model, cost), exceptions with locals, custom metrics (quota headroom, run duration, gate rejections by reason) | SQL-queryable UI over traces, alerts, LLM cost tracking without building any of it; MCP server so Claude Code can query traces |

**Will Logfire's free tier work?** Yes. Personal plan: $0, 10M spans/logs/metrics per month, 1 seat, 3 projects, no card; overage $2/M (won't happen). Our volume: a Manager run ≈ 150–300 spans (fetchers, quality checks, ~10 LLM calls, gate, orders), Review ≈ 80, watchdog ≈ 20 × 72/day ≈ 1.5k → **≈ 60–90k spans/month**, <1% of the allowance. Set `logfire.configure(scrubbing=True)` (default) and never put keys in span attributes; keep raw LLM text in the journal and send Logfire a truncated copy (5 KB/span budget).

**Instrumentation plan**
- `logfire.instrument_pydantic_ai()`, `logfire.instrument_httpx()`, `logfire.instrument_sqlite3()` (or SQLAlchemy), `structlog` → Logfire handler for human logs.
- One root span per run (`run_id` attribute); child spans per layer; `argus.gate.result`, `argus.order.placed`, `argus.watchdog.repair` as events.
- Metrics: `llm_calls_total{provider,model,agent,status}`, `provider_quota_remaining`, `run_duration_seconds`, `gate_rejections_total{reason}`, `positions_unprotected` (must be 0), `consensus_disagreements_total`.
- Alerts (Logfire alerts → Telegram webhook): run duration > 8 min, `positions_unprotected > 0`, exchange circuit breaker open, LLM error rate > 20% in an hour, no Manager run in 14h.
- Retention: Logfire default (30 days on free); the journal keeps everything.

**Built (2026-09-18).** `argus/observability/`: `setup.configure(service)` (idempotent, no-op without
`LOGFIRE_TOKEN`, httpx instrumented with `capture_headers=False` because OKX authenticates in request
headers), `instrument_engine(engine)` for the journal, `spans.span()`, `metrics.*` and `alerts.*`.
Metric names live in `metrics.py` as constants — they are the alert contract, so renaming one means
editing the Logfire alert too. Thresholds are settings: `RUN_BUDGET_SECONDS` (480) and
`MANAGER_MAX_GAP_HOURS` (14). Alert rules are pure functions over facts, tested without a clock or a
network; delivery is best-effort and never raises. `positions_unprotected` is published only when
reconcile succeeded — a 0 after an unreachable exchange would read as "all protected".

`argus doctor` checks the `LOGFIRE_TOKEN` configuration.

## 2. Guardrails

Three layers, cheapest first. All deterministic code except the last, optional one.

### 2.1 Output guardrails (Pydantic validators — mandatory)
Every LLM response is parsed into a Pydantic model with validators that encode the trading rules:
- Schema + enums + ranges (`conint(ge=-2, le=2)`, `close_pct` sums to 100, `time_stop_hours ≤ 168`).
- **Consistency:** side vs entry/stop/targets ordering; stop distance in ATR band; prices within ±15% of dossier price; symbol in exchange market list.
- **Evidence citation:** every non-zero factor score cites ≥1 existing dossier field path (checked against the dossier JSON) or is zeroed.
- **Authority limits:** LLM may not set size/leverage (fields don't exist in the schema); Reviewer's `new_sl` must be in the profit direction; `thesis_status=broken` with PnL ≤ −0.5R forces `CLOSE`.
- **Repair loop:** one repair prompt with the validation errors; then fallback model; then skip. Failures are journaled and counted (`schema_fail_rate` per model).

### 2.2 Input guardrails (code — mandatory)
- Scraped/LLM-visible text is wrapped in `<evidence>` blocks, truncated (200 chars headlines / 600 chars article excerpts), HTML stripped, control characters removed, and scanned for instruction patterns ("ignore previous", "you must", role-play markers); matches are logged as `injection_suspect` and the item is kept but flagged so the Red Team sees it.
- Dossier fields are typed numbers with provenance; strings never carry numbers the gate uses.
- Prompt versions are pinned (`prompts/*.md` with `version:`); a prompt change requires a replay diff (`CLAUDE.md`).

### 2.3 Behavioural guardrails (code — mandatory)
The Risk Gate (`DECISION_LOGIC.md` §6) is itself the guardrail on *actions*: turnover caps, size, leverage, heat, events, cooldowns, kill-switches, balance, fees. Nothing an LLM emits reaches the exchange without passing it.

### 2.4 Optional libraries
| Library | Verdict |
|---|---|
| **Guardrails AI** | Optional: its hub validators (`DetectPII`, `DetectPromptInjection`, `ValidJSON`, custom `Validator` classes) can wrap §2.1/2.2 with a shared interface and a nicer failure report. Worth adding **if** we want the validators reusable across ARGUS and the stocks bot. Not on the execution path — it runs *before* the gate, never instead of it. |
| **NeMo Guardrails** | No. Dialogue-flow (Colang) rails for chatbots; heavy, wrong shape for a batch JSON pipeline. |
| Pydantic AI `output_validator` / `result_type` | Yes — the native place for §2.1 validators. |
| Pydantic Evals | Yes — for the golden-dossier and adversarial suites (§ `LLM_AGENTS.md` §6), reported into Logfire. |

## 3. What gets analysed later (and how)
The journal + Logfire give: per-model schema pass rate and latency; per-agent token cost; gate rejection reasons over time; consensus disagreement analysis (which family was right, per regime); factor-level calibration; data-quality event frequency per source; post-mortem themes (FTS over `postmortems`). `argus report weekly` renders these into the dashboard and the Telegram `/weekly`.
