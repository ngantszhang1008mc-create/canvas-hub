-- Large compaction manifests are immutable, conversation-owned read snapshots.
CREATE TABLE assistant_context_snapshots (
    conversation_id INTEGER NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    read_id TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY(conversation_id, read_id)
);
