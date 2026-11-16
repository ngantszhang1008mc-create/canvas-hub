-- One row per external link the user asked Canvas Hub to open. A missing row
-- (or a NULL outcome) means "never inspected". Page text itself is published
-- through the ordinary importer; this table only records what happened.
CREATE TABLE link_inspections (
 source_id INTEGER PRIMARY KEY REFERENCES external_sources(id) ON DELETE CASCADE,
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 state TEXT NOT NULL DEFAULT 'idle',
 outcome TEXT,
 reason TEXT,
 login_method TEXT,
 login_host TEXT,
 http_status INTEGER,
 final_url TEXT,
 page_title TEXT,
 content_chars INTEGER,
 item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
 message TEXT,
 requested_at TEXT,
 checked_at TEXT,
 succeeded_at TEXT,
 notice_dismissed_at TEXT
);
CREATE INDEX link_inspections_account ON link_inspections(account_id, state);
