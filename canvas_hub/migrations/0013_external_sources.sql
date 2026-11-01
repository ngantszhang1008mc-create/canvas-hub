-- Registrations are account AND course scoped; legacy Canvas item identities stay intact.
CREATE TABLE external_sources (
 id INTEGER PRIMARY KEY,
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 course_id TEXT NOT NULL DEFAULT '',
 url TEXT NOT NULL,
 url_key TEXT NOT NULL,
 title TEXT NOT NULL,
 access_needed INTEGER NOT NULL DEFAULT 0,
 classification_json TEXT,
 decision TEXT NOT NULL DEFAULT 'undecided',
 analysis_request_id INTEGER REFERENCES view_requests(id) ON DELETE SET NULL,
 proposed_revision INTEGER,
 enabled_revision INTEGER,
 status TEXT NOT NULL DEFAULT 'unclassified',
 last_seen_at TEXT NOT NULL,
 last_attempt_at TEXT,
 last_success_at TEXT,
 error TEXT,
 UNIQUE(account_id, course_id, url_key)
);
CREATE TABLE external_source_origins (
 source_id INTEGER NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
 item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 PRIMARY KEY(source_id, item_id)
);
CREATE TABLE external_adapters (
 source_id INTEGER NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,
 spec_json TEXT NOT NULL,
 spec_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'proposed',
 test_json TEXT,
 approved_at TEXT,
 created_at TEXT NOT NULL,
 PRIMARY KEY(source_id, revision)
);
CREATE TABLE external_reads (
 id INTEGER PRIMARY KEY,
 source_id INTEGER NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
 revision INTEGER,
 read_at TEXT NOT NULL,
 status TEXT NOT NULL,
 evidence_json TEXT NOT NULL,
 detail TEXT
);
CREATE TABLE external_records (
 source_id INTEGER NOT NULL REFERENCES external_sources(id) ON DELETE CASCADE,
 record_key TEXT NOT NULL,
 item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 last_seen_at TEXT NOT NULL,
 PRIMARY KEY(source_id, record_key)
);
ALTER TABLE view_requests ADD COLUMN external_source_id INTEGER REFERENCES external_sources(id) ON DELETE SET NULL;
CREATE INDEX external_sources_account ON external_sources(account_id, course_id);
