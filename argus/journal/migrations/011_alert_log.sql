-- When each alert key last reached the operator (review O-3, F-141). `alerts.dispatch` repeats a
-- key only after its cooldown, so a condition that stays true is a reminder, not a flood.
-- Bookkeeping, not evidence: a plain table, not a hash-chained ledger table.

CREATE TABLE IF NOT EXISTS alert_log (
    key          TEXT PRIMARY KEY,
    severity     TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,            -- ISO-UTC; the cooldown runs from here
    last_message TEXT NOT NULL,
    suppressed   INTEGER NOT NULL DEFAULT 0 -- repeats held back since the last delivery
);
