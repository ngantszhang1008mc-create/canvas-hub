-- Tasks, checklist substeps and explicit whole-deadline markers are distinct.
ALTER TABLE annotations ADD COLUMN task_role TEXT NOT NULL DEFAULT 'task' CHECK(task_role IN ('task','substep','completion'));

-- Old versions treated every undated deadline task as a completion marker.
-- Preserve that legacy intent except tasks known to belong to checklists.
-- Current and retained historical links identify checklist tasks without titles.
UPDATE annotations SET task_role='completion'
WHERE kind='task' AND local_due IS NULL AND item_type IN ('assignment','quiz','calendar_event');
UPDATE annotations SET task_role='substep'
WHERE kind='task' AND EXISTS (
    SELECT 1 FROM components c, json_each(c.state_json, '$.links') l
    WHERE c.account_id=annotations.account_id AND c.kind='checklist'
      AND CAST(l.value AS INTEGER)=annotations.id
);
UPDATE annotations SET task_role='substep'
WHERE kind='task' AND EXISTS (
    SELECT 1 FROM component_versions v JOIN components c ON c.id=v.component_id,
         json_each(v.state_json, '$.links') l
    WHERE c.account_id=annotations.account_id AND c.kind='checklist'
      AND CAST(l.value AS INTEGER)=annotations.id
);
CREATE INDEX annotations_completion ON annotations(account_id, item_id, task_role);
