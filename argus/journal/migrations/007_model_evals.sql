-- Golden-set results, one row per model per run (LLM_AGENTS.md §6).
--
-- Kept because a single score is a snapshot and the useful signal is drift: a model that held
-- the output shape last week and fails it today has been changed underneath us, which is the
-- normal life of a free tier. Not a ledger table — it records what was measured, not what was
-- decided.
CREATE TABLE model_evals (
    eval_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    tier       TEXT NOT NULL,
    cases      INTEGER NOT NULL,
    correct    INTEGER NOT NULL,
    invented   INTEGER NOT NULL,
    schema_fail INTEGER NOT NULL,
    errors     INTEGER NOT NULL,
    p50_ms     INTEGER NOT NULL,
    usable     INTEGER NOT NULL,
    notes      TEXT
);
CREATE INDEX idx_model_evals_model ON model_evals(provider, model, created_at);
