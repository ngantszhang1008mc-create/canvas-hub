-- 0013 is reserved by the independent external-source branch. This migration
-- depends only on 0012; the migration runner applies all missing versions.
CREATE TABLE overall_widget_settings (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    time_zone TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE overall_widgets (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    widget_key TEXT NOT NULL,
    component_id INTEGER REFERENCES components(id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('pinned','conditional','library')),
    rule_json TEXT,
    position INTEGER NOT NULL,
    expanded INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    pending_json TEXT,
    PRIMARY KEY(account_id, widget_key)
);
CREATE TRIGGER overall_widget_owner_insert BEFORE INSERT ON overall_widgets
WHEN NEW.component_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM components WHERE id=NEW.component_id AND account_id=NEW.account_id
)
BEGIN
    SELECT RAISE(ABORT, 'widget component must belong to its account');
END;
CREATE TRIGGER overall_widget_owner_update BEFORE UPDATE ON overall_widgets
WHEN NEW.component_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM components WHERE id=NEW.component_id AND account_id=NEW.account_id
)
BEGIN
    SELECT RAISE(ABORT, 'widget component must belong to its account');
END;
