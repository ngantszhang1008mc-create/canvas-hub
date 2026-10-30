-- Local timetable entries (class times the user or assistant adds) and
-- requests for connected agents to read sites Canvas Hub cannot reach.

CREATE TABLE class_times (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    course_canvas_id  TEXT,
    title             TEXT NOT NULL,
    weekday           INTEGER NOT NULL,      -- 0 = Monday ... 6 = Sunday
    start_time        TEXT NOT NULL,         -- HH:MM in the account time zone
    end_time          TEXT NOT NULL,
    location          TEXT,
    valid_from        TEXT,                  -- YYYY-MM-DD, optional
    valid_until       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX class_times_account ON class_times(account_id);

CREATE TABLE view_requests (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    url               TEXT NOT NULL,
    title             TEXT,
    note              TEXT,                  -- instructions from the user, e.g. which browser profile to use
    course_canvas_id  TEXT,
    requested_by      TEXT NOT NULL,         -- user | assistant
    status            TEXT NOT NULL DEFAULT 'open',   -- open | claimed | done | declined | cancelled
    claimed_by        TEXT,
    claimed_at        TEXT,
    result_item_id    INTEGER REFERENCES items(id) ON DELETE SET NULL,
    result_note       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT
);
CREATE INDEX view_requests_account ON view_requests(account_id, status, id);
