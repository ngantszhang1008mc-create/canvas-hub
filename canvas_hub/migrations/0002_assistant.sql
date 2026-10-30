-- In-app assistant conversations (local). Messages are append-only so that
-- provider-side reasoning blocks stay valid when replayed.

CREATE TABLE assistant_conversations (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title       TEXT,
    run_state   TEXT NOT NULL DEFAULT 'idle',   -- idle | running | cancelling | error
    run_error   TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX assistant_conversations_account ON assistant_conversations(account_id, updated_at);

CREATE TABLE assistant_messages (
    id               INTEGER PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL,   -- user | assistant | tool
    content_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX assistant_messages_conv ON assistant_messages(conversation_id, id);
