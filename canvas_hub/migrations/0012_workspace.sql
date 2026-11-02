-- A scoped conversation never changes its source when the user navigates.
ALTER TABLE assistant_conversations ADD COLUMN scope_course_id TEXT;
ALTER TABLE assistant_conversations ADD COLUMN scope_item_id INTEGER REFERENCES items(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX assistant_item_scope ON assistant_conversations(account_id, scope_item_id) WHERE scope_item_id IS NOT NULL;
CREATE UNIQUE INDEX assistant_course_scope ON assistant_conversations(account_id, scope_course_id) WHERE scope_course_id IS NOT NULL AND scope_item_id IS NULL;

-- Once created, a transcript cannot be reassigned to another source or account.
CREATE TRIGGER assistant_scope_immutable BEFORE UPDATE OF account_id,scope_course_id,scope_item_id ON assistant_conversations
WHEN NEW.account_id IS NOT OLD.account_id OR NEW.scope_course_id IS NOT OLD.scope_course_id OR NEW.scope_item_id IS NOT OLD.scope_item_id
BEGIN
    SELECT RAISE(ABORT, 'conversation scope is immutable');
END;

CREATE TRIGGER assistant_scope_owner BEFORE INSERT ON assistant_conversations
WHEN (NEW.scope_item_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM items WHERE id=NEW.scope_item_id AND account_id=NEW.account_id
    AND course_canvas_id IS NEW.scope_course_id
)) OR (NEW.scope_course_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM items WHERE account_id=NEW.account_id AND item_type='course' AND source_key=NEW.scope_course_id
))
BEGIN
    SELECT RAISE(ABORT, 'conversation source must belong to its account');
END;

CREATE TABLE active_work (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    step_id TEXT,
    next_action TEXT NOT NULL DEFAULT '',
    conversation_id INTEGER REFERENCES assistant_conversations(id) ON DELETE SET NULL,
    view TEXT NOT NULL DEFAULT 'item',
    updated_at TEXT NOT NULL
);
CREATE TABLE workspace_disclosures (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    PRIMARY KEY(account_id, fingerprint)
);
CREATE TABLE workspace_action_requests (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    conversation_id INTEGER NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, request_id)
);
