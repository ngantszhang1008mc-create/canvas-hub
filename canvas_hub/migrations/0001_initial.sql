-- Canvas Hub initial schema.
-- Conventions:
--   * All timestamps are ISO-8601 UTC strings ("...Z") recorded by the app.
--   * Canvas-reported values live in item_fields / item_revisions as JSON;
--     local user data (annotations) lives in its own tables and is never
--     written by collection or import.
--   * Every Canvas-derived row is scoped to exactly one account.

CREATE TABLE instances (
    id            INTEGER PRIMARY KEY,
    origin        TEXT NOT NULL UNIQUE,          -- e.g. https://canvas.ust.hk
    label         TEXT,
    entry_url     TEXT,                          -- the address the user pasted
    created_at    TEXT NOT NULL
);

CREATE TABLE accounts (
    id                 INTEGER PRIMARY KEY,
    instance_id        INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    canvas_user_id     TEXT NOT NULL,
    display_name       TEXT,
    login_id           TEXT,
    primary_email      TEXT,
    time_zone          TEXT,                     -- IANA name as reported by Canvas
    time_zone_source   TEXT,                     -- where the time zone evidence came from
    locale             TEXT,
    first_verified_at  TEXT NOT NULL,
    last_verified_at   TEXT NOT NULL,
    scope_json         TEXT NOT NULL DEFAULT '{}',
    UNIQUE (instance_id, canvas_user_id)
);

CREATE TABLE items (
    id                     INTEGER PRIMARY KEY,
    account_id             INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    course_canvas_id       TEXT,                 -- NULL for account-level items
    item_type              TEXT NOT NULL,
    source_key             TEXT NOT NULL,        -- stable identity within (account, type)
    canvas_id              TEXT,
    source_url             TEXT,
    title                  TEXT,
    presence               TEXT NOT NULL DEFAULT 'observed',
        -- observed | not_in_latest_index | source_unavailable
    presence_detail        TEXT,
    presence_changed_at    TEXT,
    first_observed_at      TEXT NOT NULL,
    last_observed_at       TEXT NOT NULL,
    first_observed_job_id  INTEGER,
    current_revision       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, item_type, source_key)
);
CREATE INDEX items_course ON items(account_id, course_canvas_id, item_type);

-- Current merged view, one row per (item, field). last_observed_at is when
-- that specific field was last freshly read, which is what lets the UI say a
-- merged view is not uniformly fresh.
CREATE TABLE item_fields (
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    field              TEXT NOT NULL,
    value_json         TEXT NOT NULL,
    compare_hash       TEXT NOT NULL,
    first_observed_at  TEXT NOT NULL,
    last_observed_at   TEXT NOT NULL,
    last_changed_at    TEXT NOT NULL,
    method             TEXT,
    job_id             INTEGER,
    PRIMARY KEY (item_id, field)
);

-- A revision is written only when a field value changed or a field was read
-- for the first time. fields_json is the complete merged snapshot after it.
CREATE TABLE item_revisions (
    id                   INTEGER PRIMARY KEY,
    item_id              INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    revision             INTEGER NOT NULL,
    job_id               INTEGER,
    observed_at          TEXT NOT NULL,
    source_updated_at    TEXT,
    fields_json          TEXT NOT NULL,
    changed_fields_json  TEXT NOT NULL,
    added_fields_json    TEXT NOT NULL,
    recorded_at          TEXT NOT NULL,
    UNIQUE (item_id, revision)
);

-- Every observation (even unchanged ones). observation_key makes re-imports
-- of the same observation idempotent.
CREATE TABLE item_observations (
    id               INTEGER PRIMARY KEY,
    item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    job_id           INTEGER,
    observation_key  TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    method           TEXT,
    depth            TEXT,
    fields_json      TEXT NOT NULL,   -- list of field names present
    UNIQUE (item_id, observation_key)
);

-- Relationships between items (module item -> content, body link -> target,
-- folder -> file, ...). The target item stays shared; relationships carry
-- the context.
CREATE TABLE item_links (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    from_item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    to_item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    relation           TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    position           INTEGER,
    attrs_json         TEXT NOT NULL DEFAULT '{}',
    first_observed_at  TEXT NOT NULL,
    last_observed_at   TEXT NOT NULL,
    UNIQUE (from_item_id, to_item_id, relation, label)
);
CREATE INDEX item_links_to ON item_links(to_item_id);

CREATE TABLE change_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,   -- the change cursor
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    job_id             INTEGER,
    kind               TEXT NOT NULL,
        -- first_observed | field_added | field_changed
        -- not_in_latest_index | source_unavailable | reobserved
    field              TEXT,
    old_json           TEXT,
    new_json           TEXT,
    observed_at        TEXT NOT NULL,
    prior_observed_at  TEXT,
    baseline           INTEGER NOT NULL DEFAULT 0,  -- 1 when part of the account's first baseline
    recorded_at        TEXT NOT NULL
);
CREATE INDEX change_events_account ON change_events(account_id, id);
CREATE INDEX change_events_item ON change_events(item_id, id);

-- Content-addressed originals. A resource is shared storage; access always
-- goes through item_resources which carries the account scope.
CREATE TABLE resources (
    id                  TEXT PRIMARY KEY,   -- sha256 hex of the original bytes
    size                INTEGER NOT NULL,
    content_type        TEXT,
    stored_path         TEXT NOT NULL,      -- relative to the resources directory
    created_at          TEXT NOT NULL,
    extraction_status   TEXT NOT NULL DEFAULT 'not_attempted',
        -- not_attempted | ok | empty | needs_ocr | image_only | unsupported_media
        -- unsupported_type | failed | tool_unavailable | too_large
    extraction_tool     TEXT,
    extraction_detail   TEXT,
    extracted_path      TEXT,
    extracted_chars     INTEGER,
    extracted_at        TEXT
);

CREATE TABLE item_resources (
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    resource_id  TEXT NOT NULL REFERENCES resources(id),
    filename     TEXT,
    revision     INTEGER,
    observed_at  TEXT NOT NULL,
    job_id       INTEGER,
    PRIMARY KEY (item_id, resource_id)
);

CREATE TABLE jobs (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    instance_id    INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,        -- collect | import
    mode           TEXT NOT NULL,        -- baseline | thorough | quick | import
    scope_json     TEXT NOT NULL,
    state          TEXT NOT NULL,
        -- queued | running | needs_login | paused | cancel_requested | cancelled
        -- interrupted | completed | completed_with_gaps | failed
    message        TEXT,
    error          TEXT,
    stats_json     TEXT NOT NULL DEFAULT '{}',
    package_id     TEXT,                 -- for imports: idempotency key
    worker_id      TEXT,
    heartbeat_at   TEXT,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    updated_at     TEXT NOT NULL
);
CREATE INDEX jobs_account ON jobs(account_id, id);
CREATE UNIQUE INDEX jobs_package ON jobs(account_id, package_id) WHERE package_id IS NOT NULL;

-- Resumable traversal queue.
CREATE TABLE job_tasks (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    task_key          TEXT NOT NULL,
    kind              TEXT NOT NULL,
    course_canvas_id  TEXT,
    category          TEXT,
    payload_json      TEXT NOT NULL DEFAULT '{}',
    state             TEXT NOT NULL DEFAULT 'pending',
        -- pending | done | failed | blocked | skipped
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (job_id, task_key)
);
CREATE INDEX job_tasks_state ON job_tasks(job_id, state, id);

CREATE TABLE coverage (
    id                 INTEGER PRIMARY KEY,
    job_id             INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    course_canvas_id   TEXT NOT NULL DEFAULT '',   -- '' = account level
    category           TEXT NOT NULL,
    status             TEXT NOT NULL,
        -- complete | partial | blocked | not_exposed | not_checked
    index_depth        TEXT NOT NULL DEFAULT 'not_checked',
    body_depth         TEXT NOT NULL DEFAULT 'not_checked',
    attachment_depth   TEXT NOT NULL DEFAULT 'not_checked',
        -- each depth: complete | partial | not_checked | not_applicable | blocked
    expected_count     INTEGER,
    collected_count    INTEGER,
    reason             TEXT,
    detail_json        TEXT NOT NULL DEFAULT '{}',   -- filters, date ranges, pending destinations
    mode               TEXT NOT NULL,
    observed_at        TEXT NOT NULL,
    UNIQUE (job_id, course_canvas_id, category)
);
CREATE INDEX coverage_lookup ON coverage(account_id, course_canvas_id, category, id);

CREATE TABLE discrepancies (
    id                 INTEGER PRIMARY KEY,
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    other_item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL,
    detail_json        TEXT NOT NULL,
    first_detected_at  TEXT NOT NULL,
    last_detected_at   TEXT NOT NULL,
    UNIQUE (item_id, other_item_id, kind)
);

-- Local user data. Never modified by collection or observation import.
CREATE TABLE annotations (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    item_id      INTEGER REFERENCES items(id) ON DELETE SET NULL,
    item_type    TEXT,          -- denormalized so a note survives even if the item row is removed
    item_source_key TEXT,
    course_canvas_id TEXT,
    kind         TEXT NOT NULL,   -- note | task
    title        TEXT,
    body         TEXT NOT NULL DEFAULT '',
    task_state   TEXT,            -- open | done (local only, never a Canvas claim)
    local_due    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX annotations_account ON annotations(account_id, item_id);

-- Full-text search. search_docs maps FTS rowids to items/resources.
CREATE TABLE search_docs (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    resource_id  TEXT NOT NULL DEFAULT '',
    UNIQUE (item_id, resource_id)
);
CREATE VIRTUAL TABLE search_fts USING fts5(
    title, body, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
