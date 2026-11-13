-- Phase 1 core tables (ARCHITECTURE.md §10). LLM/analysis tables arrive with Prompt 7;
-- protection and journal come before intelligence (BUILD_PLAN.md).
-- ANSI types only + ISO-UTC TEXT timestamps so the Postgres move is a connection string
-- (MEMORY_AND_STATE.md §3). Ledger tables carry prev_hash/row_hash and are insert-only.

CREATE TABLE runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,            -- manager | review | watchdog | reflect | replay
    mode         TEXT NOT NULL,            -- demo | auto | approve
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,            -- RUNNING | OK | FAILED | HALTED
    shadow       INTEGER NOT NULL DEFAULT 0,
    config_hash  TEXT NOT NULL,
    error        TEXT
);

CREATE TABLE plans (
    plan_id      TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    mode         TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT,                     -- long | short | NULL for NO_TRADE
    state        TEXT NOT NULL,            -- PlanState
    payload      TEXT NOT NULL,            -- validated TradePlan JSON
    step         INTEGER,                  -- small-account step used (DECISION_LOGIC §5.1)
    supersedes_id TEXT,
    prev_hash    TEXT NOT NULL,
    row_hash     TEXT NOT NULL
);

CREATE TABLE gate_results (
    gate_result_id TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL REFERENCES plans(plan_id),
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    verdict      TEXT NOT NULL,            -- APPROVED | ADJUSTED | REJECTED
    reasons      TEXT NOT NULL,            -- JSON list of {gate, verdict, reason, before, after}
    supersedes_id TEXT,
    prev_hash    TEXT NOT NULL,
    row_hash     TEXT NOT NULL
);

CREATE TABLE orders (
    order_id     TEXT PRIMARY KEY,         -- our id
    cl_ord_id    TEXT NOT NULL UNIQUE,     -- idempotency key: run_id[:8]+plan_id[:8]
    exchange_id  TEXT,
    algo_id      TEXT,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    plan_id      TEXT REFERENCES plans(plan_id),
    created_at   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    kind         TEXT NOT NULL,            -- entry | sl | tp | trailing | close | reduce
    order_type   TEXT NOT NULL,            -- limit | market | conditional | move_order_stop
    price        TEXT,                     -- Decimal as TEXT; never float money
    size         TEXT NOT NULL,
    state        TEXT NOT NULL,            -- OrderState
    expires_at   TEXT,                     -- entry TTL (F-028)
    raw_request  TEXT NOT NULL,
    raw_response TEXT,
    supersedes_id TEXT,
    prev_hash    TEXT NOT NULL,
    row_hash     TEXT NOT NULL
);

CREATE TABLE fills (
    fill_id      TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    exchange_fill_id TEXT,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    price        TEXT NOT NULL,
    size         TEXT NOT NULL,
    fee          TEXT NOT NULL,            -- realised, signed as the exchange reports it (F-036)
    fee_ccy      TEXT NOT NULL,
    raw          TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    row_hash     TEXT NOT NULL
);

CREATE TABLE positions_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    size         TEXT NOT NULL,
    entry_price  TEXT NOT NULL,
    mark_price   TEXT NOT NULL,
    leverage     TEXT NOT NULL,
    margin       TEXT NOT NULL,
    unrealised   TEXT NOT NULL,
    liq_price    TEXT,
    state        TEXT NOT NULL,            -- PositionState
    protection   TEXT NOT NULL,            -- ProtectionState
    plan_id      TEXT REFERENCES plans(plan_id),
    raw          TEXT NOT NULL
);

CREATE TABLE position_events (
    event_id     TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    plan_id      TEXT REFERENCES plans(plan_id),
    machine      TEXT NOT NULL,            -- position | protection | plan | order
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    detail       TEXT,
    prev_hash    TEXT NOT NULL,
    row_hash     TEXT NOT NULL
);

CREATE TABLE kill_switches (
    kill_switch_id TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    scope        TEXT NOT NULL,            -- plan | portfolio
    symbol       TEXT,
    rule         TEXT NOT NULL,
    triggered    INTEGER NOT NULL DEFAULT 0,
    detail       TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    row_hash     TEXT NOT NULL
);

CREATE TABLE circuit_breaker_events (
    event_id     TEXT PRIMARY KEY,
    run_id       TEXT REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    dependency   TEXT NOT NULL,
    from_state   TEXT NOT NULL,
    to_state     TEXT NOT NULL,            -- closed | open | half_open
    attempts     INTEGER NOT NULL,         -- attempts, not successes (F-029)
    detail       TEXT
);

CREATE TABLE data_quality_events (
    event_id     TEXT PRIMARY KEY,
    run_id       TEXT REFERENCES runs(run_id),
    created_at   TEXT NOT NULL,
    trap         TEXT NOT NULL,            -- DATA_SOURCES.md §9 trap id
    source       TEXT NOT NULL,
    symbol       TEXT,
    severity     TEXT NOT NULL,            -- info | suspect | reject
    detail       TEXT NOT NULL
);

CREATE INDEX idx_plans_run ON plans(run_id);
CREATE INDEX idx_orders_run ON orders(run_id);
CREATE INDEX idx_orders_plan ON orders(plan_id);
CREATE INDEX idx_fills_order ON fills(order_id);
CREATE INDEX idx_snapshots_symbol ON positions_snapshots(symbol, created_at);
CREATE INDEX idx_events_symbol ON position_events(symbol, created_at);
