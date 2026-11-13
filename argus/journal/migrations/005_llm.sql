-- LLM call log and per-provider quota counters (LLM_AGENTS.md §5).
--
-- `llm_calls` is the evidence trail: every prompt, every raw answer, whether it validated. It is
-- what makes a bad decision explainable a month later, and it is the dataset for the weekly
-- model evaluation. Not a ledger table — it records what was *said*, not what was decided.
CREATE TABLE llm_calls (
    call_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    agent        TEXT NOT NULL,            -- regime | analyst | redteam | trader | ...
    tier         TEXT NOT NULL,            -- fast | strong_json | reasoning | paid_fallback
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    family       TEXT NOT NULL,            -- consensus pairing must cross families
    attempt      INTEGER NOT NULL,         -- 1 = first choice in the tier
    status       TEXT NOT NULL,            -- ok | schema_fail | error | timeout | rate_limited
    latency_ms   INTEGER,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    prompt_version TEXT,
    prompt       TEXT,                     -- full text, so a replay needs no network
    raw_output   TEXT,                     -- including reasoning, before validation
    error        TEXT
);
CREATE INDEX idx_llm_calls_run ON llm_calls(run_id, created_at);
CREATE INDEX idx_llm_calls_model ON llm_calls(provider, model, created_at);

-- One row per provider per UTC day. Free tiers are rationed by the day, and a run that discovers
-- its quota by being refused has already wasted the call (LLM_AGENTS.md §5: skip at 90% of RPD).
CREATE TABLE llm_quota (
    provider    TEXT NOT NULL,
    day         TEXT NOT NULL,             -- UTC date, YYYY-MM-DD
    calls       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day)
);
