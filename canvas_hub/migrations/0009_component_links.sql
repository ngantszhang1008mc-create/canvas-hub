-- Links are local to an account; service validation checks course/item ownership.
ALTER TABLE components ADD COLUMN course_id TEXT;
ALTER TABLE components ADD COLUMN item_id INTEGER REFERENCES items(id) ON DELETE SET NULL;
CREATE INDEX components_course ON components(account_id, course_id);
CREATE INDEX components_item ON components(account_id, item_id);
