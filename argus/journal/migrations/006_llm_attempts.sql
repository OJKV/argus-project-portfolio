-- Split out of 005 after 005 had already been applied on the VPS (F-085). A migration that
-- has run is history: the only way to change a deployed schema is a new file.
--
-- IF NOT EXISTS because the split is retroactive: a journal created from the edited 005
-- already has this table, while one created from the original 005 does not. Both must end
-- up in the same place, and neither may fail.
-- Per-minute rate limiting needs timestamps, not a daily counter. Kept separate from `llm_calls`
-- because an attempt is recorded *before* the call returns — the window has to include calls that
-- are still in flight, or a burst slips through while every request is pending.
CREATE TABLE IF NOT EXISTS llm_attempts (
    attempt_id TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_attempts_window ON llm_attempts(provider, created_at);
