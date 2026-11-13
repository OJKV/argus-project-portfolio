-- What the adversarial chain decided about each Trader plan (`runs/pipeline.vet`, review L-10).
-- A plan the chain blocks never reaches `plans`, so until this table a block was a console line
-- and nobody could measure how often a rejection rule fires on honest plans. Not a ledger table:
-- like `llm_calls` it is the evidence trail, while the decisions that trade live in
-- `plans` / `gate_results`. Numbered 012 because 010/011 are taken on another branch.
CREATE TABLE IF NOT EXISTS vettings (
    vetting_id    TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    created_at    TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    trader_family TEXT NOT NULL,           -- the family the Consensus and Red Team had to avoid
    blocked_by    TEXT,                    -- trader | evidence | consensus | redteam | riskdebate; NULL = passed
    size_factor   REAL NOT NULL,           -- 0 when blocked
    checks        TEXT NOT NULL,           -- JSON [[agent, outcome], ...] in the order they ran
    unsupported   TEXT NOT NULL,           -- JSON {factor: [cited paths]} the evidence check refused
    plan          TEXT                     -- the TradePlan as validated, so the check can be re-run
);
CREATE INDEX IF NOT EXISTS idx_vettings_run ON vettings(run_id);
