-- Each chat request records the uncorrected text-length estimate beside what the
-- provider reported, keyed by provider, endpoint and model. Later estimates for
-- the same model are corrected with that ratio.
ALTER TABLE assistant_context_usage ADD COLUMN raw_estimated_tokens INTEGER;
ALTER TABLE assistant_context_usage ADD COLUMN calibration_key TEXT;
CREATE INDEX assistant_usage_calibration ON assistant_context_usage(calibration_key, id);
