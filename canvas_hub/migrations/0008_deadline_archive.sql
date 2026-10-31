-- Local deadline state survives subsequent Canvas observations.
CREATE TABLE deadline_archives (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    deadline_key TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (account_id, deadline_key)
);
