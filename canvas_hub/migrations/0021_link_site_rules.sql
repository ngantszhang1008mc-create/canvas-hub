-- The user's choice for every link from one site: read them without asking, or
-- ignore them. A site without a row is asked about (or ignored by a built-in
-- default for video, social and reference sites). An explicit 'ask' row overrides
-- such a default.
CREATE TABLE link_site_rules (
 account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 site TEXT NOT NULL,
 decision TEXT NOT NULL CHECK (decision IN ('ask', 'read', 'ignore')),
 updated_at TEXT NOT NULL,
 PRIMARY KEY (account_id, site)
);
