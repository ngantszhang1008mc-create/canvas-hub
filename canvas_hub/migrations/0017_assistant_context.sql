-- History stays append-only. A checkpoint only changes the provider projection.
CREATE TABLE assistant_context (
    conversation_id INTEGER PRIMARY KEY REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    through_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    data_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE assistant_context_usage (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES assistant_conversations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,
    usage_json TEXT,
    estimated_input_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
ALTER TABLE assistant_conversations ADD COLUMN context_notice TEXT;

-- Derived, locally rebuildable index. Deleting/replacing a search document also
-- removes its excerpts, so old attachment revisions cannot remain current hits.
CREATE TABLE context_sources (
    search_doc_id INTEGER PRIMARY KEY REFERENCES search_docs(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    preview TEXT NOT NULL,
    total_chars INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL
);
CREATE TABLE context_chunks (
    id INTEGER PRIMARY KEY,
    search_doc_id INTEGER NOT NULL REFERENCES context_sources(search_doc_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    section TEXT,
    page INTEGER,
    text TEXT NOT NULL,
    UNIQUE(search_doc_id, ordinal)
);
CREATE VIRTUAL TABLE context_chunk_fts USING fts5(text, tokenize='unicode61');
CREATE TRIGGER context_chunk_delete AFTER DELETE ON context_chunks BEGIN
    DELETE FROM context_chunk_fts WHERE rowid=OLD.id;
END;
