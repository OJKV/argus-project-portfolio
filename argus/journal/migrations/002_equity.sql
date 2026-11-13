-- Equity history for the portfolio drawdown kill-switch (DECISION_LOGIC.md §9).
-- Not a ledger table: it is measurement, not decision, so it carries no hash chain.
CREATE TABLE equity_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    created_at  TEXT NOT NULL,
    equity      TEXT NOT NULL
);

CREATE INDEX idx_equity_created ON equity_snapshots(created_at);
