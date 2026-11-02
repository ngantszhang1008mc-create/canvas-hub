-- One disclosure is one durable attempt, including failed/cancelled attempts.
CREATE TABLE source_analysis_attempts (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id INTEGER REFERENCES view_requests(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'offered'
);
CREATE UNIQUE INDEX source_analysis_one_running ON source_analysis_attempts(source_id) WHERE state='running';
CREATE TABLE source_analysis_batches (
    token TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    scope_key TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'offered'
);
CREATE UNIQUE INDEX source_batch_one_running ON source_analysis_batches(account_id,scope_key) WHERE state='running';
