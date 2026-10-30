-- Interactive components the assistant creates (score models, checklists, ...)
-- and the state the user gives them (ticks, skipped lines, partial values).
-- Every state change keeps the previous version so it can be undone.

CREATE TABLE components (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    conversation_id   INTEGER REFERENCES assistant_conversations(id) ON DELETE SET NULL,
    kind              TEXT NOT NULL,          -- score_model | checklist
    title             TEXT NOT NULL,
    spec_json         TEXT NOT NULL,          -- the definition (buckets, rows, ...)
    state_json        TEXT NOT NULL DEFAULT '{}',
    version           INTEGER NOT NULL DEFAULT 1,
    pinned            INTEGER NOT NULL DEFAULT 0,
    created_by        TEXT NOT NULL DEFAULT 'assistant',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX components_account ON components(account_id, pinned);

CREATE TABLE component_versions (
    component_id      INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    version           INTEGER NOT NULL,
    spec_json         TEXT NOT NULL,
    state_json        TEXT NOT NULL,
    changed_by        TEXT NOT NULL,          -- user | assistant
    created_at        TEXT NOT NULL,
    PRIMARY KEY (component_id, version)
);
