-- Reuse the durable summary-attempt ledger for ordinary assistant requests.
-- Existing rows remain summaries; telemetry is never a quota settlement ledger.
ALTER TABLE assistant_context_usage ADD COLUMN purpose TEXT NOT NULL DEFAULT 'summary';
CREATE INDEX assistant_usage_conversation ON assistant_context_usage(conversation_id, purpose, id);
