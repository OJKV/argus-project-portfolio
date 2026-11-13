-- One post-mortem per closed trade (LLM_AGENTS.md §3.28), keyed by the plan that opened it.
--
-- Stored rather than recomputed because the Reflector reads thirty of these at once and the
-- judgement must be the one made when the trade closed, on the evidence available then. Recomputed
-- later it would be graded against a market that has since resolved, which is the hindsight the
-- post-mortem prompt spends most of its length trying to prevent.
--
-- Not a ledger table: it records a reading, not a decision. The decisions are in `plans` and
-- `position_events`, and `outcome_r` is copied here only so one query can hand the Reflector a
-- trade and its verdict together.
CREATE TABLE postmortems (
    plan_id         TEXT PRIMARY KEY REFERENCES plans(plan_id),
    created_at      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    outcome_r       REAL,
    exit_reason     TEXT,
    process_verdict TEXT NOT NULL,          -- sound | flawed
    would_take_again INTEGER NOT NULL,
    entry_timing    TEXT NOT NULL,
    sizing          TEXT NOT NULL,
    stop_placement  TEXT NOT NULL,
    -- The computed facts the verdict was formed against, so a later reader can see whether the
    -- judgement matched the arithmetic without re-deriving it (F-111).
    flags           TEXT,
    report          TEXT NOT NULL           -- full PostMortem JSON
);
CREATE INDEX idx_postmortems_created ON postmortems(created_at);
