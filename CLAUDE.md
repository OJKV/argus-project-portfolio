# CLAUDE.md — instructions for Claude Code in the ARGUS repo

You are working on ARGUS, an LLM-assisted swing-trading agent for OKX USDT perpetuals. Tool-agnostic rules live in `AGENTS.md` (root); this file adds Claude Code specifics. Read `docs/ARCHITECTURE.md` and `docs/DECISION_LOGIC.md` before touching anything under `argus/gate/`, `argus/exchange/`, or `argus/prompts/`. `docs/LLM_AGENTS.md` governs prompts. `docs/FAILURE_LOG.md` lists mistakes already made — do not repeat them.

**Tiered context (keep sessions cheap):** Tier 1 = this file (always). Tier 2 = the one doc for the area you're changing. Tier 3 = specific source files on request. Do not load the whole `docs/` set per session.

## Non-negotiables (safety)
1. **Never place, modify or cancel a live order from a test, script, notebook, or "quick check".** Live execution happens only inside `argus manager` / `argus review` / `argus watchdog` with `MODE in {approve, auto}` and `LIVE_TRADING=I_UNDERSTAND` — and, for the real-money account (`OKX_DEMO=false`), `LIVE_VENUE=I_UNDERSTAND_REAL_MONEY` as well; `argus/config.py` refuses the live account without it. Tests use `MODE=demo` or the fake adapter.
   A manual trade requires an explicit request in the current session and confirmation of the
   account, instrument and size. Use the demo venue unless live execution is explicitly requested
   and its interlocks are already set. Use the minimum permitted size, attach a stop and journal
   the order. Never infer authorization from a schedule or this document.
   **`MODE=approve` is not a safety step.** It has no approval queue yet: it places orders exactly
   like `MODE=auto` — no QUEUED plan, no `/approve`, nothing waits for owner (review 2026-10-07, P-N2).
   The ARMED banner and `argus doctor` say so; never describe an approve-mode run as "waiting".

2. **Never read or print secrets.** `.env` is off-limits; use `argus.config.Settings`. Never log API keys, passphrases, or Telegram tokens; never commit `.env`, `*.db`, `journal/`.
3. **The Risk Gate is code, not prompts.** Do not add a way for LLM output to change size, leverage, stop widening, or gate thresholds. If a prompt change needs a gate change, edit `argus/gate/` with tests and say so in the PR description.
4. **Every position must have an exchange-side stop.** Any change to `place_bracket`, `amend_stop`, or `reconcile` requires the watchdog integration test (`tests/exchange/test_watchdog_repair.py`) to pass on the OKX demo env.
5. **No new paid dependency or API** without an entry in `docs/DATA_SOURCES.md` and a cost line in `docs/ARCHITECTURE.md` §12.
6. **Scraped/LLM text is untrusted.** It never becomes a parameter without schema validation.
7. **No LiteLLM** (supply-chain incident, March 2026). LLM transport is Pydantic AI + `argus/llm/router.py`; observability via Logfire (`logfire.instrument_pydantic_ai()`).

## Repo layout
```
argus/
  cli.py                # argus manager|review|watchdog|reflect|replay|dossier|regime|universe|doctor|ui|eval-*|halt|resume|flatten
  config.py             # pydantic-settings; MODE, interlocks, risk params, schedules, model tiers
  doctor.py             # `argus doctor` checks (keys, venue, interlocks, runs, live keys)
  data/                 # fetchers/<source>.py, cache.py, ratelimit.py, quality.py (18 traps), envelope.py, scrub.py, consensus.py
  features/             # regime.py, universe.py (the screen), dossier.py, indicators.py, market.py, risk_flags.py, review_flags.py, trade_flags.py
  llm/                  # router.py (Pydantic AI models, quotas, circuit breakers), schemas.py, golden.py, agents/{regime,analyst,redteam,riskdebate,trader,trader_batch,consensus,reviewer,postmortem,reflector,catalyst}.py
  prompts/              # *.md with `version:` header; changes require a replay diff
  gate/                 # rules.py (14 gates), sizing.py (+ small-account steps), protections.py, killswitch.py, fees.py
  exchange/             # base.py (interface), okx.py (ccxt; build_bracket_params), fake.py (tests), reconcile.py, breakers.py, bybit.py (planned: KZ execution fallback; Bybit is a data fetcher today)
  execution/            # orders.py (bracket construction), idempotency.py, place.py, resting.py (entry TTL/drift), lifecycle.py
  journal/              # db.py, migrations/, states.py (state machines), ledger.py (hash chain), replay.py, positions.py, trades.py, lessons.py, models.py (planned), calibration.py (planned), shadow.py (planned)
  ui/                   # FastAPI + HTMX dashboard (templates/), read-only, loopback only
  observability/        # setup.py (logfire), metrics.py, alerts.py, spans.py
  notify/               # telegram.py (alerts; the command router has no poller yet)
  runs/                 # manager.py, pipeline.py, review.py, watchdog.py, reflect.py, lock.py (the run lock)
knowledge/              # sources.md, catalysts.yaml, tokenomics.yaml, wallets.yaml, blocklist.yaml
tests/                  # unit/, data/, data_quality/, features/, gate/, exchange/, execution/, journal/, runs/, agents/ (golden dossiers), deploy/, integration/
docs/                   # this design set
deploy/                 # systemd/*.timer|*.service, install.sh, install-units.sh, update.sh, backup.sh, checks.sh, trade.sh
```

## Conventions
- Python 3.12, `uv` (hash-locked `uv.lock`), `ruff` (line length 100), `mypy --strict` on `gate/`, `exchange/`, `execution/`, `journal/states.py`.
- Two-gate verification before "done": Gate 1 `ruff && mypy && pytest`; Gate 2 the six questions in `AGENTS.md`.
- Pydantic v2 models for every boundary (fetcher out, dossier, LLM in/out, gate, orders). No raw dicts across module boundaries.
- Sync code (ccxt sync) inside a run; runs are short-lived processes; `asyncio.gather` only inside `data/` for parallel fetches.
- Every fetcher returns `Observation`; every number in a dossier keeps provenance.
- Prices/quantities as `Decimal` in `execution/`; floats allowed in analysis.
- Log with `structlog`, JSON to file, human to console; include `run_id`.
- Timestamps: UTC everywhere; convert only in Telegram output (Almaty, UTC+5).
- Prompts: edit the `.md`, bump `version:`, run `uv run argus replay --since 14d --prompt-version <new>` (planned — not implemented: `replay --prompt-version` exits 2 and `--since` does not exist; until it lands, run `argus eval-agents --agent <role>` before and after and say in the PR that no replay diff was possible) and paste the diff summary into the PR/commit message.

## Commands
```
uv sync
uv run argus doctor                       # connectivity + quotas
uv run pytest -q                          # unit (no network)
uv run pytest -q -m demo                  # needs OKX demo key (MODE=demo)
uv run argus dossier BTC --print
uv run argus manager --shadow             # no orders, journal only
uv run argus review --dry-run              # argus-review.service runs `argus review` armed (no --dry-run)
uv run argus watchdog --once
uv run argus replay <run_id> | --last [--json]   # --prompt-version v is (planned)
uv run argus backtest-rules --from 2024-09-01 --to 2026-09-01 --symbols BTC,ETH,SOL,ARB   # (planned: a stub that exits 2)
uv run argus eval-models                  # golden dossiers × all providers (weekly)
uv run argus eval-agents [--agent X]      # one role's own golden suite — quality per role
uv run argus ui --port 8787               # dashboard (bind 127.0.0.1)
uv run argus audit                        # verify ledger hash chain (planned: J-N1; until then RUNBOOK 'Ledger mismatch')
```

## When asked to "improve the strategy"
Do not tune gate numbers from intuition. Propose the change, show the rule-layer backtest and/or replay diff and calibration impact, and update `docs/DECISION_LOGIC.md` with the *why*. Record anything learned the hard way in `docs/FAILURE_LOG.md`, and update `docs/STATUS.md`.

## When something fails in production
1. Reproduce with `argus replay <run_id>` (no new API calls).
2. Write the `FAILURE_LOG.md` entry first (symptom, cause, fix, test added).
3. Fix + regression test; never "patch the prompt" for a code bug.

## Stress-test the decision before you take it (grilling skill)
Invoke the `grilling` skill — and tell owner you are doing it — whenever the conversation reaches one of
these three gates. Do not wait to be asked; owner can also trigger it with "grill me on X" or `/grilling`.
1. **Going live with real money** (Phase 4 → Phase 5): the moment demo results are said to justify
   $100–200 of real capital.
2. **Changing any gate threshold.** Every number marked **[tune]** moves on evidence, never on taste.
3. **Accepting a conclusion from backtest or replay results** — before it becomes a design change.

## Token discipline (measured, not guessed)
On 2026-09-18 a full session here was 409k tokens: tool-call **inputs** 55%, tool results 27%,
two screenshots 11%. The dominant cost was Claude pasting whole files into edit payloads.
- **Edit with the smallest unique anchor** (`Edit`, or `sed -i ''`). Never rewrite a file to change a
  few lines; never paste back code that is already on disk. `Write` is for new files only.
- **Read narrowly**: `grep -n`, `sed -n '40,80p'`, `rg -A3` — not a whole module to see one function.
- **Trim output**: `| tail -20`, `-q`, `--short`, `--stat`.
- **Delegate breadth**: a subagent or `~/.agents/skills/pi-delegate` for "find/list/summarise" work;
  keep design, debugging and anything unverifiable in the main thread.
- Screenshots cost ~23k tokens each. Paste text when text will do.
