-- What the system believes it has learned (LLM_AGENTS.md §3.7).
--
-- Insert-only, and deliberately so. A promoted lesson is rendered into the Trader's and Analyst's
-- prompts for sixty days, which makes it part of the reason every trade taken in that window was
-- taken. Overwriting a lesson would erase the explanation for decisions already in the ledger, so
-- a reflection run only ever appends: reconfirmation is a new row with the same fingerprint, and
-- a contradiction is a new row naming the trades that broke it.
--
-- Nothing here is authoritative on its own. "Active" is derived in `argus/journal/lessons.py` by
-- counting evidence across rows, so promotion stays code the model cannot reach — the same
-- division as `order_state`, which is derived from `position_events` rather than stored.
CREATE TABLE lessons (
    lesson_id     TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    -- Normalised text, so the same claim proposed in different words in a later run accumulates
    -- evidence instead of competing with itself for one of the ten prompt slots.
    fingerprint   TEXT NOT NULL,
    lesson        TEXT NOT NULL,
    scope         TEXT NOT NULL,
    supporting    TEXT NOT NULL,   -- JSON array of trade ids
    contradicting TEXT NOT NULL,   -- JSON array of trade ids
    confidence_pct INTEGER NOT NULL,
    -- Why a candidate was refused, when it was: an unsafe lesson and an ungrounded one are
    -- different failures and the difference is worth keeping.
    rejected_for  TEXT
);
CREATE INDEX idx_lessons_fingerprint ON lessons(fingerprint, created_at);
