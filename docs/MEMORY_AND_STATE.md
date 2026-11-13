# MEMORY_AND_STATE.md — options reviewed, decisions, migration paths

Your note asked to review all four layers from the Scira doc: short-term (LangGraph), long-term (vector DB), portfolio state (Postgres), trade journal (immutable ledger). Each is evaluated against ARGUS's shape: batch runs, ~2 trades/day, one VPS, one operator.

## 1. Short-term / run state

| Option | What it gives | Cost | Verdict |
|---|---|---|---|
| **LangGraph checkpoints** (Scira; TradingAgents `--checkpoint`) | Resume a multi-node graph after a crash mid-run; per-node state | LangGraph + LangChain dependency, graph DSL, another persistence format | **No.** A Manager run is ~10 sequential typed function calls over ~6 minutes; if it dies, the next run (≤ 6h later) re-derives everything from fresh data — resuming a stale half-run is *wrong*, not helpful |
| **Run context object + journal rows** (ARGUS) | `RunContext(run_id, mode, config_snapshot)` passed through layers; every stage writes its output row before the next starts; `argus replay <run_id>` reconstructs any stage from rows | zero extra deps | **Yes.** Crash-safety = "every completed stage is persisted"; partial runs are marked `ABORTED` and never place orders |
| Pydantic AI agent runs | Typed inputs/outputs, tool calls, Logfire spans per run | already in stack | **Yes**, as the call layer inside each stage |

## 2. Long-term memory

| Option | What it gives | Cost | Verdict |
|---|---|---|---|
| **Vector DB** (Chroma/Qdrant) with embeddings of past theses | "Find similar past situations" retrieval | embedding calls, another service, semantic drift, hard to audit why a memory was surfaced | **Not in MVP.** With <500 trades/year, exact retrieval beats fuzzy: same asset, same regime, same factor pattern — all are columns |
| **SQLite FTS5 + structured filters** (ARGUS) | `lessons`, `postmortems`, `analyses` searchable by asset/regime/factor/text; `/why` and Reflector retrieval | zero | **Yes** |
| **TradingAgents decision log** pattern | Same-ticker recent decisions + cross-ticker lessons, outcome-grounded, injected into the PM prompt | one short call per closed trade | **Yes** — adopted as `postmortems` (5 same-asset + 5 cross-asset injected) |
| **Verbal feedback (Scira v2 §4.4)** | Natural-language critiques instead of weight updates | same call | Same thing; adopted |
| Embeddings later | If the journal exceeds ~2k trades or we add stocks, add an `embeddings` column (any free embedding model) and cosine search *alongside* FTS — additive, no migration | later | Roadmap |

Guardrail on memory: a lesson becomes *active* only with ≥3 supporting trades and expires after 60 days unless reconfirmed; max 10 active lessons + 10 post-mortems per prompt. Memory that fits noise is worse than no memory.

## 3. Portfolio state & journal storage

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **SQLite (WAL)** | one file, zero ops, transactional (atomic state transitions), FTS5, `.backup`, trivially replayable, fits a €5 box | single-writer (fine: runs are serialised by lock; UI is read-mostly); no remote access without a tunnel | **MVP** |
| **PostgreSQL** (Scira) | concurrent writers, remote access, richer types, MCP servers for Postgres exist, pgvector later | a service to run/patch/back up; ~0.5–1 GB RAM on the VPS; no benefit until there are multiple writers | **Migration target**, not MVP. Trigger: UI needs concurrent writes, second bot instance (stocks) shares the DB, or you want a hosted journal MCP |
| Managed Postgres (Neon/Supabase free) | free tier, remote, MCP-ready | egress latency from VPS, vendor free-tier changes, secrets over the wire | Option for the stocks bot / SaaS phase |
| DuckDB / Parquet for analytics | fast columnar queries over the journal | not for OLTP | Nice-to-have: nightly export of journal tables to Parquet for notebooks |

Design constraints that make the SQLite→Postgres move a connection-string change: SQLAlchemy Core with ANSI types only; UUID strings; timestamps as ISO UTC; JSON columns as TEXT with validated Pydantic payloads; no SQLite-only pragmas in migrations; FTS5 isolated behind a `search()` function (Postgres version uses `tsvector`).

## 4. Trade journal as an immutable ledger

- Ledger tables (`plans`, `gate_results`, `orders`, `fills`, `position_events`, `reviews`, `postmortems`, `kill_switches`) are **insert-only**; corrections are new rows with `supersedes_id`.
- Each ledger row stores `prev_hash` (SHA-256 of the previous row in that table) → tamper-evident chain; `argus audit` verifies it.
- Every decision row references the exact `dossier_id` and `llm_call_id` it consumed; every LLM call stores the full prompt, raw reasoning/text and parsed output (Logfire keeps a copy as spans; the journal is the system of record).
- Nightly `sqlite3 .backup` + copy to a second location; monthly Parquet export.

## 5. Journal MCP (your idea, post-MVP)
A read-only MCP server over the journal (tools: `get_trade`, `why`, `calibration`, `search_lessons`, `recent_runs`) lets Claude Code / any client interrogate results without SQL. Cheap to build with Pydantic AI's MCP support once the schema is stable; scheduled for roadmap Phase 7. Write tools are deliberately excluded.

## 6. Decision summary
Run state: RunContext + journal rows (no LangGraph). Long-term: SQLite FTS + structured lessons/post-mortems (no vector DB in MVP; embeddings additive later). Storage: SQLite WAL now, Postgres-ready schema, migrate on a concrete trigger. Ledger: insert-only, hash-chained, fully replayable. Observability copy in Logfire.
