-- Reminders the user or the assistant set for a to-do or a Canvas item.
-- The app shows them (toast, bell, optional desktop notification) while it runs.
CREATE TABLE reminders (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    note              TEXT,
    remind_at         TEXT NOT NULL,          -- ISO-8601 UTC
    item_id           INTEGER REFERENCES items(id) ON DELETE SET NULL,
    annotation_id     INTEGER REFERENCES annotations(id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending | fired | dismissed
    fired_at          TEXT,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX reminders_due ON reminders(account_id, status, remind_at);
