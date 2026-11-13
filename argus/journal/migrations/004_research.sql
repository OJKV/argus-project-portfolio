-- Research artefacts of a Manager run (ARCHITECTURE.md §10). Not ledger tables: they record what
-- was *seen*, while the decisions taken from them live in `plans` / `gate_results`.
CREATE TABLE regime_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    created_at  TEXT NOT NULL,
    label       TEXT NOT NULL,
    risk_mult   REAL NOT NULL,
    limit_only  INTEGER NOT NULL,
    inputs      TEXT NOT NULL,   -- RegimeInputs JSON
    reasons     TEXT NOT NULL,
    modifiers   TEXT NOT NULL,
    gaps        TEXT NOT NULL
);

CREATE TABLE dossiers (
    dossier_id  TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    created_at  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    source      TEXT NOT NULL,   -- core | whitelist | satellite
    tokens      INTEGER NOT NULL,
    trade_critical INTEGER NOT NULL,
    payload     TEXT NOT NULL,   -- exactly what an LLM would be shown
    gaps        TEXT NOT NULL
);

CREATE INDEX idx_dossiers_run ON dossiers(run_id);
CREATE INDEX idx_dossiers_symbol ON dossiers(symbol, created_at);
