# ARGUS — design package (v1.0, 2026-09-14)

LLM-assisted, evidence-graded swing-trading agent for OKX USDT perpetuals (1–7d, 2–10x), scheduled runs, free data/LLM tiers, ~€6–10/month.

Read in this order:
1. `docs/ARCHITECTURE.md` — the design (start with §0 design thesis)
2. `docs/DECISION_LOGIC.md` — regime, scoring, sizing (+ small-account mode), gates, trailing/review rules
3. `docs/UNIVERSE_OPTIONS.md` — **decision pending:** which assets to research/trade
4. `docs/DATA_SOURCES.md` — every API/source, tiers, limits, the 18 traps, verdict on every API you named
5. `docs/LLM_AGENTS.md` — trading LLM roles, contracts, router, harness
6. `docs/LLM_PROVIDERS_AND_MODELS.md` — free providers to add, router tests, trading-tuned models, what "best model" means
7. `docs/MEMORY_AND_STATE.md` · `docs/OBSERVABILITY_AND_GUARDRAILS.md` · `docs/UI_DESIGN.md`
8. `docs/BUILD_PLAN.md` (phases 0–6, gates) · `docs/ROADMAP.md` (7+) · `docs/STRATEGY_STACKS.md` · `docs/SIDE_PROJECTS.md`
9. `docs/OPEN_QUESTIONS.md` — resolved log + N1–N7 still open · `docs/NAMING.md`
10. `docs/REVIEW-scira-doc-and-prototype.md` — keep/replace from prior work (v1, v2, best-practices, prototype)
11. `docs/FAILURE_LOG.md` · `docs/STATUS.md` · `docs/SAFETY.md`
12. `AGENTS.md` (coding agents: Codex/Antigravity/OpenCode) · `CLAUDE.md` (Claude Code)
14. `docs/design_docs_changelog.md` — what changed in each doc and why

Core stance: the LLM proposes; code sizes, gates, executes; every number carries provenance; every position has an exchange-side stop and a trailing stop; every decision is replayable.
