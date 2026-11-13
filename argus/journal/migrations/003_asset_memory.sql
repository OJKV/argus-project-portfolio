-- Rolling per-asset memory cards (ARCHITECTURE.md §11). Derived and rebuilt every Manager run,
-- so not a ledger table: the card is measurement, the decisions it summarises live in `plans`.
CREATE TABLE asset_memory (
    symbol      TEXT PRIMARY KEY,
    updated_at  TEXT NOT NULL,
    card        TEXT NOT NULL
);
