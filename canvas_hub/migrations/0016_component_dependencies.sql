-- Indexed dependency selection avoids refreshing every component after each import.
-- Deliberately independent of migration 0015 (parallel source-analysis feature).
CREATE TABLE component_source_dependencies (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    dependency_kind TEXT NOT NULL CHECK(dependency_kind IN ('item','collection')),
    dependency_key TEXT NOT NULL,
    PRIMARY KEY(component_id,dependency_kind,dependency_key)
);
CREATE INDEX component_source_dependency_lookup ON component_source_dependencies(account_id,dependency_kind,dependency_key,component_id);
-- Imported observations and their pending refresh keys commit together. A crash
-- after import but before component refresh leaves a replayable local outbox.
CREATE TABLE component_source_changes (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    dependency_kind TEXT NOT NULL CHECK(dependency_kind IN ('item','collection')),
    dependency_key TEXT NOT NULL,
    PRIMARY KEY(account_id,dependency_kind,dependency_key)
);
INSERT OR IGNORE INTO component_source_dependencies(account_id,component_id,dependency_kind,dependency_key)
SELECT c.account_id,c.id,'item',CAST(json_extract(b.value,'$.item_id') AS TEXT)
    FROM components c,json_each(c.source_config_json,'$.bindings') b
    UNION
    SELECT c.account_id,c.id,'item',CAST(json_extract(r.value,'$.item_id') AS TEXT)
    FROM components c,json_each(c.source_config_json,'$.review_sources') r
    UNION
    SELECT c.account_id,c.id,'collection',CAST(json_extract(c.source_config_json,'$.collection.course_id') AS TEXT)
    FROM components c  WHERE json_extract(c.source_config_json,'$.collection.course_id') IS NOT NULL;
CREATE TRIGGER component_source_dependencies_insert AFTER INSERT ON components
BEGIN
    DELETE FROM component_source_dependencies WHERE component_id=NEW.id;
    INSERT OR IGNORE INTO component_source_dependencies(account_id,component_id,dependency_kind,dependency_key)
    SELECT c.account_id,c.id,'item',CAST(json_extract(b.value,'$.item_id') AS TEXT)
    FROM components c,json_each(c.source_config_json,'$.bindings') b WHERE c.id=NEW.id
    UNION
    SELECT c.account_id,c.id,'item',CAST(json_extract(r.value,'$.item_id') AS TEXT)
    FROM components c,json_each(c.source_config_json,'$.review_sources') r WHERE c.id=NEW.id
    UNION
    SELECT c.account_id,c.id,'collection',CAST(json_extract(c.source_config_json,'$.collection.course_id') AS TEXT)
    FROM components c WHERE c.id=NEW.id AND json_extract(c.source_config_json,'$.collection.course_id') IS NOT NULL;
END;
CREATE TRIGGER component_source_dependencies_update AFTER UPDATE OF source_config_json,account_id ON components
BEGIN
    DELETE FROM component_source_dependencies WHERE component_id=NEW.id;
    INSERT OR IGNORE INTO component_source_dependencies(account_id,component_id,dependency_kind,dependency_key)
    SELECT c.account_id,c.id,'item',CAST(json_extract(b.value,'$.item_id') AS TEXT)
    FROM components c,json_each(c.source_config_json,'$.bindings') b WHERE c.id=NEW.id
    UNION
    SELECT c.account_id,c.id,'item',CAST(json_extract(r.value,'$.item_id') AS TEXT)
    FROM components c,json_each(c.source_config_json,'$.review_sources') r WHERE c.id=NEW.id
    UNION
    SELECT c.account_id,c.id,'collection',CAST(json_extract(c.source_config_json,'$.collection.course_id') AS TEXT)
    FROM components c WHERE c.id=NEW.id AND json_extract(c.source_config_json,'$.collection.course_id') IS NOT NULL;
END;
