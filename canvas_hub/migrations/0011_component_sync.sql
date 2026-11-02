-- Source declarations, observations and local choices have separate storage.
ALTER TABLE components ADD COLUMN purpose_key TEXT;
ALTER TABLE components ADD COLUMN source_config_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE components ADD COLUMN source_values_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE components ADD COLUMN source_status_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE components ADD COLUMN source_overrides_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE components ADD COLUMN pending_spec_json TEXT;
ALTER TABLE component_versions ADD COLUMN source_meta_json TEXT;
CREATE UNIQUE INDEX components_purpose ON components(
    account_id, kind, COALESCE(course_id,''), COALESCE(item_id,-1), purpose_key
) WHERE purpose_key IS NOT NULL;
