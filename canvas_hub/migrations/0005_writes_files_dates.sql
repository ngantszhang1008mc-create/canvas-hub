-- Phase 2: confirmed writes to Canvas, saved files, dates found in course
-- text, links between duplicate course shells, and per-account settings.

-- Writes to Canvas are off by default. Each one is prepared first (with an
-- exact preview), sent only after the user confirms it in the app, and kept
-- here as a log.
CREATE TABLE canvas_writes (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,          -- submission_text | submission_file | discussion_reply | message
    course_canvas_id  TEXT NOT NULL,
    target_item_id    INTEGER REFERENCES items(id) ON DELETE SET NULL,
    target_canvas_id  TEXT,                   -- assignment / discussion topic id
    target_title      TEXT,
    payload_json      TEXT NOT NULL,          -- body text, recipients, subject, file reference
    preview_hash      TEXT NOT NULL,          -- sha256 of what the preview showed; confirm must match
    status            TEXT NOT NULL DEFAULT 'prepared',  -- prepared | sending | sent | failed | cancelled | expired
    prepared_by       TEXT NOT NULL,          -- assistant | user
    conversation_id   INTEGER,
    result_json       TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    confirmed_at      TEXT,
    finished_at       TEXT
);
CREATE INDEX canvas_writes_account ON canvas_writes(account_id, status);

-- Files the assistant or the user saved (finished artefacts, upload sources).
CREATE TABLE saved_files (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    filename          TEXT NOT NULL,
    mime              TEXT NOT NULL,
    size              INTEGER NOT NULL,
    sha256            TEXT NOT NULL,
    content           BLOB NOT NULL,
    is_text           INTEGER NOT NULL DEFAULT 1,
    course_canvas_id  TEXT,
    created_by        TEXT NOT NULL,          -- assistant | user
    conversation_id   INTEGER,
    created_at        TEXT NOT NULL
);
CREATE INDEX saved_files_account ON saved_files(account_id);

-- Dates found in page/announcement/syllabus text (exams, quizzes, ...) that
-- are not Canvas deadlines. Suggestions until the user confirms or dismisses.
CREATE TABLE date_candidates (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    item_id           INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    course_canvas_id  TEXT,
    label             TEXT NOT NULL,
    starts_at         TEXT NOT NULL,          -- ISO-8601 with offset
    ends_at           TEXT,
    location          TEXT,
    snippet           TEXT NOT NULL,
    fingerprint       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'suggested',   -- suggested | confirmed | dismissed
    annotation_id     INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (account_id, fingerprint)
);

-- The user's word that two course shells are the same course (or not).
CREATE TABLE course_links (
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    course_canvas_id  TEXT NOT NULL,
    other_canvas_id   TEXT NOT NULL,
    relation          TEXT NOT NULL,          -- same | different
    created_at        TEXT NOT NULL,
    PRIMARY KEY (account_id, course_canvas_id, other_canvas_id)
);

CREATE TABLE account_settings (
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    key               TEXT NOT NULL,
    value_json        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);
