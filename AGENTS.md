# AGENTS.md — guide for coding agents (Codex, Antigravity, OpenCode, Cursor, Claude Code)

This repository is ARGUS, an LLM-assisted swing-trading agent for OKX USDT perpetuals. `CLAUDE.md` holds Claude Code specifics; this file is the tool-agnostic contract. Trading-side LLM agents (Analyst, Trader, …) are documented in `docs/LLM_AGENTS.md` — not here.

## Read first
`docs/ARCHITECTURE.md` (§0 thesis, §7 gate, §8 execution) → `docs/DECISION_LOGIC.md` → `docs/FAILURE_LOG.md` (do not repeat listed failures) → the doc for the area you touch.

## Hard rules (identical for every agent)
1. No live orders from tests/scripts/notebooks. Armed execution only via `argus manager|review|watchdog` with `MODE=auto|approve` and `LIVE_TRADING=I_UNDERSTAND`; `LIVE_TRADING` alone only arms the **demo** account — the live account (`OKX_DEMO=false`) is refused unless `LIVE_VENUE=I_UNDERSTAND_REAL_MONEY` is also set (`argus/config.py`). Never set either word. Tests use `MODE=demo` or `FakeExchange`.
2. Never read/print/commit secrets (`.env`, keys, tokens, `*.db`, `journal/`). Use `argus.config.Settings`.
3. The Risk Gate is code. No change may let LLM output set size, leverage, widen a stop, or bypass a gate. Gate changes require tests + a `docs/DECISION_LOGIC.md` update with the *why*.
4. Every position must have an exchange-side stop; changes to `exchange/`, `execution/`, `runs/watchdog.py` must pass `tests/exchange/test_watchdog_repair.py` on OKX demo.
5. No new paid dependency/API (budget rule: only VPS + paid LLM fallback). New free sources need a `docs/DATA_SOURCES.md` row.
6. Scraped or LLM text is untrusted data; it becomes a parameter only through Pydantic validation.
7. No LiteLLM. LLM transport is Pydantic AI + `argus/llm/router.py`.

## Two-gate verification before you say "done" (from the Polyphemus case study)
Gate 1 (automated): `uv run ruff check . && uv run mypy argus/gate argus/exchange argus/execution && uv run pytest -q`.
Gate 2 (answer in the PR/commit message): Does this do exactly what was asked — not more? Are external calls using the correct endpoints/params (check the OKX/ccxt docs, not memory)? Is error handling present on every I/O call? Any hardcoded value that belongs in config? Does it break anything that worked? Can every line be explained tomorrow?

## Working style
- Small PRs; one concern each. Prompts: edit `argus/prompts/*.md`, bump `version:`, run `argus replay --since 14d --prompt-version <v>` (planned — not implemented yet; until then `argus eval-agents --agent <role>` before and after, and say so), paste the diff summary.
- Numbers in docs marked **[tune]** change only with backtest/replay/calibration evidence, never by taste.
- Reproduce production issues with `argus replay <run_id>` before changing code; write the `FAILURE_LOG.md` entry first.
- Edit with the smallest unique anchor; never rewrite a whole file for a few lines and never
  paste back code already on disk. Read narrowly (`grep -n`, `sed -n 'a,bp'`), trim output
  (`| tail`, `-q`), delegate broad searches to a subagent. Context is a budget like any other.
- Update `docs/STATUS.md` when a milestone moves.

## Repo map, commands, conventions
See `CLAUDE.md` (layout, `uv` commands, ruff/mypy settings, Decimal in execution, UTC everywhere).
