"""Declarative, local component synchronization. No evaluation of source code or AI.

The component spec is the materialized definition. Bindings, last observed source
values, explicit local overrides, and the user's interactive state are independent.
Stable paths address IDs, never numeric positions or arbitrary JSON pointers.
"""

from __future__ import annotations

import copy
import json
import math
import re

from ..htmltools import to_text
from ..util import Conflict, InvalidInput, NotFound, canonical_json, now_iso, parse_iso, sha256_text

SOURCE_META = ("source_config_json", "source_values_json", "source_status_json",
               "source_overrides_json", "pending_spec_json")
SOURCE_FIELDS = frozenset({"title", "name", "due_at", "deadline", "points_possible", "score",
                           "score_status", "target", "minimum", "points", "threshold"})
REVIEW_FIELDS = frozenset({"body", "description", "syllabus_body"})
_ID = re.compile(r"^[\w.:-]{1,64}$")


def source_meta(row):
    return {key: row.get(key) for key in SOURCE_META}


def target_slot(kind, spec, target):
    """Return a validated leaf container and name; never traverse arbitrary keys."""
    if not isinstance(target, str) or len(target) > 300:
        raise InvalidInput("source target must be a stable component field path")
    parts = target.split("/")
    tops = {"score_model": {"title", "threshold"}, "checklist": {"title"},
            "grade_projector": {"title", "target"}, "hours_tracker": {"title", "deadline", "target", "minimum", "points"}}
    if len(parts) == 1 and parts[0] in tops[kind]:
        return spec, parts[0]
    lists = None
    if kind == "score_model" and len(parts) == 3 and parts[0] == "buckets" and parts[2] in {"label", "max", "earned", "status", "due"}:
        lists = spec["buckets"]
    elif kind == "checklist" and len(parts) == 5 and parts[0] == "sections" and parts[2] == "rows" and parts[4] in {"label", "points", "due"}:
        section = next((s for s in spec["sections"] if s["id"] == parts[1]), None)
        lists = section["rows"] if section else []
    elif kind == "grade_projector" and len(parts) == 5 and parts[0] == "categories" and parts[2] == "items" and parts[4] in {"label", "max", "score"}:
        category = next((s for s in spec["categories"] if s["id"] == parts[1]), None)
        lists = category["items"] if category else []
    if lists is None or any(not _ID.fullmatch(p) for p in parts):
        raise InvalidInput("unsupported source target")
    found = next((v for v in lists if v["id"] == parts[-2]), None)
    if found is None:
        raise InvalidInput("source target does not exist in this component")
    return found, parts[-1]


class ComponentSourceSync:
    """Mixin using ComponentService's ownership, normalization and version storage.

    Bindings are {target, item_id, field}; SOURCE_FIELDS is the complete direct
    field allowlist. Dates use a stored ISO scalar or the ``iso`` date member.
    Submission score_status uses explicit excused/graded/known ungraded facts;
    an unavailable grade never produces lost status or a zero score.
    No expressions, JSON paths, formulas or remote fetches run during sync.
    ``source_values`` records {value, item_id, field, observed_at} per target.
    ``source_status`` exposes availability (available/partial), unavailable
    details, needs_review, last_attempt_at (local sync time), last_success_at
    (oldest actual observation in the last wholly successful refresh), and
    review fingerprints. Partial reads retain the last successful timestamp.
    Overrides carry {value, updated_at}; follow_source=True removes one.
    """

    def _source_item(self, account_id, item_id):
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            raise InvalidInput("source item_id must be a local integer ID")
        item = self.db.one("SELECT * FROM items WHERE account_id=? AND id=?", (account_id, item_id))
        if not item:
            raise NotFound("source item not found for this account", code="item_not_found")
        return dict(item)

    def _validate_sources(self, account_id, kind, spec, config, course_id=None):
        if not isinstance(config, dict) or set(config) - {"bindings", "collection", "review_sources"}:
            raise InvalidInput("sources must contain only bindings, collection and review_sources")
        if len(canonical_json(config)) > 200_000:
            raise InvalidInput("source configuration is too large")
        bindings, targets = [], set()
        raw = config.get("bindings", [])
        if not isinstance(raw, list) or len(raw) > 1000:
            raise InvalidInput("bindings must be a list of at most 1000 fields")
        for binding in raw:
            if not isinstance(binding, dict) or set(binding) != {"target", "item_id", "field"}:
                raise InvalidInput("each binding needs target, item_id and field")
            target_slot(kind, spec, binding["target"])
            if not isinstance(binding["field"], str) or binding["field"] not in SOURCE_FIELDS:
                raise InvalidInput("unsupported source field")
            source_item = self._source_item(account_id, binding["item_id"])
            if binding["field"] == "score_status" and source_item["item_type"] != "submission":
                raise InvalidInput("score_status requires a local submission source")
            if binding["target"].endswith("/status") and binding["field"] != "score_status":
                raise InvalidInput("bucket status requires the derived score_status source field")
            if binding["target"] in targets:
                raise InvalidInput("a target can have only one source binding")
            targets.add(binding["target"])
            bindings.append(dict(binding))
        collection = config.get("collection")
        if collection is not None:
            if kind not in ("checklist", "score_model"):
                raise InvalidInput("assignment collections require a checklist or score model")
            if not isinstance(collection, dict) or set(collection) != {"course_id", "item_type", "section_id"}:
                raise InvalidInput("collection needs course_id, item_type and section_id")
            if collection["item_type"] != "assignment" or not isinstance(collection["section_id"], str) or not _ID.fullmatch(collection["section_id"]):
                raise InvalidInput("collection must select assignments and a stable section ID")
            ccourse, _ = self.validate_links(account_id, collection["course_id"])
            if ccourse is None or (course_id is not None and ccourse != course_id):
                raise InvalidInput("collection course must match the component course")
            collection = {**collection, "course_id": ccourse}
        review = config.get("review_sources", [])
        if not isinstance(review, list) or len(review) > 100:
            raise InvalidInput("review_sources must be a list of at most 100 items")
        clean_review, seen = [], set()
        for source in review:
            if not isinstance(source, dict) or set(source) - {"item_id", "fields", "attachments"} or "item_id" not in source:
                raise InvalidInput("review sources need item_id, fields and optional attachments")
            self._source_item(account_id, source["item_id"])
            fields = source.get("fields", [])
            if not isinstance(fields, list) or any(not isinstance(f, str) or f not in REVIEW_FIELDS for f in fields):
                raise InvalidInput("review source fields must be body, description or syllabus_body")
            if not isinstance(source.get("attachments", False), bool) or not fields and not source.get("attachments"):
                raise InvalidInput("a review source needs fields or attachments")
            if source["item_id"] in seen:
                raise InvalidInput("duplicate review source item")
            seen.add(source["item_id"])
            clean_review.append({"item_id": source["item_id"], "fields": sorted(set(fields)),
                                 "attachments": source.get("attachments", False)})
        return {"bindings": bindings, "collection": collection, "review_sources": clean_review}

    def configure_sources(self, account_id, cid, sources):
        with self._lock, self.db.tx():
            row = self._row(account_id, cid)
            spec = json.loads(row["spec_json"])
            config = self._validate_sources(account_id, row["kind"], spec, sources, row["course_id"])
            status = json.loads(row["source_status_json"])
            old = json.loads(row["source_config_json"])
            # A new declaration establishes a baseline; reconfiguring the same
            # evidence must not silently approve a rule change already detected.
            old_review_ids = {s["item_id"] for s in old.get("review_sources", [])}
            kept = {str(s["item_id"]) for s in config["review_sources"] if s["item_id"] in old_review_ids}
            for key in ("review_snapshots", "review_fingerprints", "accepted_fingerprints"):
                status[key] = {k: v for k, v in status.get(key, {}).items() if k in kept}
            if not kept:
                status["needs_review"] = False
            active_targets = {b["target"] for b in config["bindings"]}
            collection = config["collection"]
            if collection:
                sid = collection["section_id"]
                if row["kind"] == "checklist":
                    entries = [r for s in spec["sections"] if s["id"] == sid for r in s["rows"]]
                    active_targets.update(f"sections/{sid}/rows/{r['id']}/{leaf}" for r in entries
                                          if r["id"] == f"canvas:{r.get('item_id')}" for leaf in ("label", "points", "due"))
                else:
                    active_targets.update(f"buckets/{b['id']}/{leaf}" for b in spec["buckets"]
                                          if b["id"] == f"canvas:{b.get('item_id')}" for leaf in ("label", "max", "due", "earned", "status"))
            # Detached fields become ordinary local definition values; stale
            # observations or overrides must not keep controlling them.
            detached = {key: canonical_json({target: value for target, value in json.loads(row[key]).items()
                                             if target in active_targets})
                        for key in ("source_values_json", "source_overrides_json")}
            stored_config = config if config["bindings"] or config["collection"] or config["review_sources"] else {}
            self._save(account_id, row, spec, json.loads(self._effective_state_json(account_id, row)), "source_config",
                       source_changes={"source_config_json": canonical_json(stored_config), "source_status_json": canonical_json(status),
                                       "pending_spec_json": None, **detached})
            return self._sync_component(account_id, self._row(account_id, cid))

    def _read_source(self, account_id, item_id, field):
        item = self.db.one("SELECT * FROM items WHERE account_id=? AND id=?", (account_id, item_id))
        if not item or item["presence"] != "observed":
            return None, "source_unavailable"
        if field == "score_status":
            return self._read_score_status(item)
        observed = self.db.one("SELECT value_json,last_observed_at FROM item_fields WHERE item_id=? AND field=?", (item_id, field))
        if not observed:
            return None, "field_unavailable"
        value = json.loads(observed["value_json"])
        if field in {"due_at", "deadline"} and isinstance(value, dict):
            value = value.get("iso")
        if value is None or value == "":
            return None, "field_unavailable"
        if isinstance(value, (dict, list, bool)):
            return None, "unsupported_value"
        if observed["last_observed_at"] < item["last_observed_at"]:
            return None, "field_not_refreshed"
        return {"value": value, "item_id": item_id, "field": field,
                "observed_at": observed["last_observed_at"]}, None

    def _read_score_status(self, item):
        """Only explicit, fresh submission facts can change a bucket's actual status."""
        if item["item_type"] != "submission":
            return None, "unsupported_source_type"
        fields = {r["field"]: (json.loads(r["value_json"]), r["last_observed_at"]) for r in self.db.query(
            "SELECT field,value_json,last_observed_at FROM item_fields WHERE item_id=? AND field IN ('score','workflow_state','excused')", (item["id"],))}
        def fresh(name):
            return name in fields and fields[name][1] >= item["last_observed_at"]
        if fresh("excused") and fields["excused"][0] is True:
            value, observed_at = "excused", fields["excused"][1]
        elif not fresh("workflow_state"):
            return None, "field_not_refreshed"
        elif fields["workflow_state"][0] in ("unsubmitted", "submitted", "pending_review"):
            value, observed_at = "pending", fields["workflow_state"][1]
        elif fields["workflow_state"][0] == "graded":
            if not fresh("score"):
                return None, "field_not_refreshed"
            score = fields["score"][0]
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or score < 0:
                return None, "field_unavailable"
            value = "earned"
            observed_at = min(fields["score"][1], fields["workflow_state"][1], key=parse_iso)
        else:
            return None, "field_unavailable"
        return {"value": value, "item_id": item["id"], "field": "score_status", "observed_at": observed_at}, None

    def _review_status(self, account_id, config, status, unavailable, source_times):
        snapshots = status.setdefault("review_snapshots", {})
        fingerprints = status.setdefault("review_fingerprints", {})
        accepted = status.setdefault("accepted_fingerprints", {})
        changed = []
        for source in config.get("review_sources", []):
            iid, key = source["item_id"], str(source["item_id"])
            item = self.db.one("SELECT presence,last_observed_at FROM items WHERE account_id=? AND id=?", (account_id, iid))
            if not item or item["presence"] != "observed":
                unavailable.append({"item_id": iid, "reason": "review_source_unavailable"})
                continue
            previous = snapshots.get(key, {})
            fields = {name: value for name, value in previous.get("fields", {}).items() if name in source["fields"]}
            observations = {r["field"]: r for r in self.db.query(
                "SELECT field,value_json,last_observed_at FROM item_fields WHERE item_id=?", (iid,))}
            current = {name: json.loads(r["value_json"]) for name, r in observations.items()}
            read = False
            for field in source["fields"]:
                if field in observations and observations[field]["last_observed_at"] < item["last_observed_at"]:
                    unavailable.append({"item_id": iid, "field": field, "reason": "review_field_not_refreshed"})
                    continue
                if field in current and isinstance(current[field], str):
                    fields[field] = to_text(current[field])
                    source_times.append(observations[field]["last_observed_at"])
                    read = True
                elif field in fields:
                    unavailable.append({"item_id": iid, "field": field, "reason": "review_field_unavailable"})
            resource_ids = previous.get("resource_ids", [])
            linked_ids = previous.get("linked_item_ids", [])
            if source["attachments"]:
                resources = self.db.query(
                    """SELECT ir.resource_id,ir.observed_at,i.id AS item_id,i.presence,i.last_observed_at
                       FROM item_resources ir JOIN items i ON i.id=ir.item_id
                       WHERE i.account_id=? AND (i.id=? OR i.id IN (
                         SELECT l.to_item_id FROM item_links l JOIN items t ON t.id=l.to_item_id
                         WHERE l.account_id=? AND l.from_item_id=? AND (l.relation='attachment' OR t.item_type='file')))
                       AND ir.observed_at=(SELECT MAX(current.observed_at) FROM item_resources current WHERE current.item_id=ir.item_id)
                    """, (account_id, iid, account_id, iid))
                attachment_gaps = []
                for resource in resources:
                    if resource["presence"] != "observed":
                        attachment_gaps.append({"item_id": resource["item_id"], "reason": "review_attachment_unavailable"})
                    elif resource["observed_at"] < resource["last_observed_at"]:
                        attachment_gaps.append({"item_id": resource["item_id"], "reason": "review_attachment_not_refreshed"})
                linked = self.db.query("""SELECT t.id FROM item_links l JOIN items t ON t.id=l.to_item_id
                                          WHERE l.account_id=? AND l.from_item_id=? AND t.account_id=?
                                          AND (l.relation='attachment' OR t.item_type='file')""", (account_id, iid, account_id))
                linked_ids = sorted({target["id"] for target in linked})
                links_removed = bool(set(previous.get("linked_item_ids", [])) - set(linked_ids))
                resource_item_ids = {r["item_id"] for r in resources}
                attachment_gaps.extend({"item_id": target["id"], "reason": "review_attachment_unavailable"}
                                       for target in linked if target["id"] not in resource_item_ids)
                if not resources and previous.get("resource_ids") and not links_removed:
                    attachment_gaps.append({"item_id": iid, "reason": "review_attachment_unavailable"})
                if attachment_gaps:
                    unavailable.extend(attachment_gaps)
                    resource_ids = previous.get("resource_ids", sorted({r["resource_id"] for r in resources}))
                else:
                    resource_ids = sorted({r["resource_id"] for r in resources})
                    source_times.extend(r["observed_at"] for r in resources)
                if resource_ids or links_removed:
                    read = True
            if not read:
                unavailable.append({"item_id": iid, "reason": "review_evidence_unavailable"})
                continue
            snapshot = {"fields": fields, "resource_ids": resource_ids, "linked_item_ids": linked_ids}
            fingerprint = sha256_text(canonical_json({"fields": fields, "resource_ids": resource_ids}))
            snapshots[key], fingerprints[key] = snapshot, fingerprint
            if key not in accepted:
                accepted[key] = fingerprint
            elif accepted[key] != fingerprint:
                changed.append(iid)
        status["changed_review_sources"] = changed
        status["needs_review"] = bool(changed) or bool(status.get("needs_review"))

    def _collection_bindings(self, account_id, row, spec, config, unavailable, source_times):
        collection = config.get("collection")
        if not collection:
            return []
        course, section_id = collection["course_id"], collection["section_id"]
        coverage = self.db.one("""SELECT status,index_depth,observed_at FROM coverage WHERE account_id=?
                                AND course_canvas_id=? AND category='assignments' ORDER BY id DESC LIMIT 1""", (account_id, course))
        if not coverage or coverage["status"] != "complete" or coverage["index_depth"] != "complete":
            unavailable.append({"course_id": course, "reason": "collection_partial"})
        if coverage:
            source_times.append(coverage["observed_at"])
        items = self.db.query("SELECT id,presence FROM items WHERE account_id=? AND course_canvas_id=? AND item_type='assignment' ORDER BY id", (account_id, course))
        if row["kind"] == "checklist":
            section = next((s for s in spec["sections"] if s["id"] == section_id), None)
            if section is None:
                section = {"id": section_id, "title": "Assignments", "rows": []}
                spec["sections"].append(section)
            entries = section["rows"]
        else:
            entries = spec["buckets"]
        known_ids = {i["id"] for i in items}
        for entry in entries:
            iid = entry.get("item_id")
            if entry["id"] == f"canvas:{iid}" and iid not in known_ids:
                unavailable.append({"item_id": iid, "reason": "source_unavailable"})
        bindings = []
        for item in items:
            iid, stable_id = item["id"], f"canvas:{item['id']}"
            entry = next((e for e in entries if e["id"] == stable_id), None)
            # Never collide with or convert a manually authored entry.
            if entry is not None and entry.get("item_id") != iid:
                unavailable.append({"item_id": iid, "reason": "collection_id_conflict"})
                continue
            if item["presence"] != "observed":
                unavailable.append({"item_id": iid, "reason": "source_unavailable"})
                continue
            title, title_error = self._read_source(account_id, iid, "title")
            maximum, max_error = self._read_source(account_id, iid, "points_possible")
            if entry is None:
                if title_error or row["kind"] == "score_model" and max_error:
                    unavailable.append({"item_id": iid, "reason": "collection_fields_unavailable"})
                    continue
                entry = {"id": stable_id, "label": title["value"], "item_id": iid}
                if row["kind"] == "score_model":
                    entry.update(max=maximum["value"], status="pending", group=section_id)
                candidate = copy.deepcopy(spec)
                candidate_entries = (next(s for s in candidate["sections"] if s["id"] == section_id)["rows"]
                                     if row["kind"] == "checklist" else candidate["buckets"])
                candidate_entries.append(entry)
                try:
                    self.normalize(row["kind"], candidate)
                except InvalidInput:
                    unavailable.append({"item_id": iid, "reason": "invalid_collection_value"})
                    continue
                entries.append(entry)
            prefix = f"sections/{section_id}/rows/{stable_id}" if row["kind"] == "checklist" else f"buckets/{stable_id}"
            for field, leaf in (("title", "label"), ("points_possible", "points" if row["kind"] == "checklist" else "max"), ("due_at", "due")):
                bindings.append({"target": f"{prefix}/{leaf}", "item_id": iid, "field": field})
            if row["kind"] == "score_model":
                submission = self.db.one("""SELECT s.id FROM items s LEFT JOIN item_links l
                    ON l.account_id=s.account_id AND l.from_item_id=s.id AND l.to_item_id=? AND l.relation='submission_for'
                    WHERE s.account_id=? AND s.item_type='submission' AND (l.id IS NOT NULL OR
                      (s.course_canvas_id=? AND s.canvas_id=(SELECT canvas_id FROM items WHERE id=?)))
                    ORDER BY CASE WHEN l.id IS NOT NULL THEN 0 ELSE 1 END,s.id LIMIT 1""", (iid, account_id, course, iid))
                if submission:
                    bindings.extend({"target": f"{prefix}/{leaf}", "item_id": submission["id"], "field": field}
                                    for field, leaf in (("score", "earned"), ("score_status", "status")))
                else:
                    unavailable.append({"item_id": iid, "reason": "submission_unavailable"})
        return bindings

    def _sync_component(self, account_id, row):
        config = json.loads(row["source_config_json"])
        spec, status = json.loads(row["spec_json"]), json.loads(row["source_status_json"])
        values = json.loads(row["source_values_json"])
        overrides = json.loads(row["source_overrides_json"])
        unavailable, source_times = [], []
        self._review_status(account_id, config, status, unavailable, source_times)
        bindings = self._collection_bindings(account_id, row, spec, config, unavailable, source_times)
        # Explicit bindings take precedence over automatic collection fields.
        bindings = {b["target"]: b for b in bindings + config.get("bindings", [])}
        observations = {}
        for target, binding in bindings.items():
            observed, reason = self._read_source(account_id, binding["item_id"], binding["field"])
            if reason:
                unavailable.append({**binding, "reason": reason})
                continue
            observations[target] = observed
        # Related fields (e.g. target and minimum hours) describe one snapshot.
        # Validate the whole snapshot so a valid pair never depends on list order.
        candidate = copy.deepcopy(spec)
        try:
            for target, observed in observations.items():
                container, leaf = target_slot(row["kind"], candidate, target)
                container[leaf] = observed["value"]
            candidate = self.normalize(row["kind"], candidate)
            for target, override in overrides.items():
                container, leaf = target_slot(row["kind"], candidate, target)
                container[leaf] = override["value"]
            candidate = self.normalize(row["kind"], candidate)
        except InvalidInput:
            candidate = None
        if candidate is not None:
            spec = candidate
            for target, observed in observations.items():
                values[target] = observed
                source_times.append(observed["observed_at"])
            observations = {}
        # If some source values are malformed, still refresh independent fields.
        for target, observed in observations.items():
            binding = bindings[target]
            try:
                candidate = copy.deepcopy(spec)
                container, leaf = target_slot(row["kind"], candidate, target)
                container[leaf] = observed["value"]
                clean = self.normalize(row["kind"], candidate)
            except InvalidInput:
                unavailable.append({**binding, "reason": "invalid_source_value"})
                continue
            values[target] = observed
            source_times.append(observed["observed_at"])
            if target not in overrides:
                spec = clean
        # Overrides are explicit, persist through imports and can be followed again.
        for target, override in overrides.items():
            try:
                container, leaf = target_slot(row["kind"], spec, target)
                container[leaf] = override["value"]
            except InvalidInput:
                unavailable.append({"target": target, "reason": "override_target_unavailable"})
        spec = self.normalize(row["kind"], spec)
        status.update(last_attempt_at=now_iso(), unavailable=unavailable,
                      availability="partial" if unavailable else "available")
        if not unavailable and source_times:
            status["last_success_at"] = min(source_times, key=lambda value: parse_iso(value))
        else:
            status.setdefault("last_success_at", None)
        changes = {"source_status_json": canonical_json(status), "source_values_json": canonical_json(values)}
        if spec != json.loads(row["spec_json"]):
            before = self._effective_state_json(account_id, row)
            state = self._clean_state(row["kind"], spec, json.loads(before))
            if row["kind"] == "checklist" and spec.get("sync_todos"):
                state = self._push_todos(account_id, spec, state, previous=json.loads(row["spec_json"]))
            return self._save(account_id, row, spec, state, "source_sync", before, source_changes=changes)
        with self.db.tx() as conn:
            conn.execute("UPDATE components SET source_status_json=?,source_values_json=? WHERE id=? AND account_id=? AND version=?",
                         (changes["source_status_json"], changes["source_values_json"], row["id"], account_id, row["version"]))
        return self.get(account_id, row["id"])

    def sync_sources(self, account_id):
        with self._lock, self.db.tx():
            rows = self.db.query("SELECT * FROM components WHERE account_id=? AND source_config_json!='{}' ORDER BY id", (account_id,))
            return [self._sync_component(account_id, dict(row)) for row in rows]

    def set_source_override(self, account_id, cid, target, value=None, follow_source=False):
        if not isinstance(follow_source, bool):
            raise InvalidInput("follow_source must be a boolean")
        with self._lock, self.db.tx():
            row = self._row(account_id, cid)
            spec = json.loads(row["spec_json"])
            container, leaf = target_slot(row["kind"], spec, target)
            values, overrides = json.loads(row["source_values_json"]), json.loads(row["source_overrides_json"])
            config = json.loads(row["source_config_json"])
            if target not in values and not any(b["target"] == target for b in config.get("bindings", [])):
                raise InvalidInput("only a source-bound field can be overridden")
            if follow_source:
                overrides.pop(target, None)
                if target in values:
                    container[leaf] = values[target]["value"]
            else:
                container[leaf] = value
                overrides[target] = {"value": value, "updated_at": now_iso()}
            spec = self.normalize(row["kind"], spec)
            before = self._effective_state_json(account_id, row)
            state = self._clean_state(row["kind"], spec, json.loads(before))
            if row["kind"] == "checklist" and spec.get("sync_todos"):
                state = self._push_todos(account_id, spec, state, previous=json.loads(row["spec_json"]))
            return self._save(account_id, row, spec, state, "source_override", before,
                              source_changes={"source_overrides_json": canonical_json(overrides)})

    def propose_spec(self, account_id, cid, spec):
        with self._lock, self.db.tx():
            row = self._row(account_id, cid)
            clean = self.normalize(row["kind"], spec)
            self._validate_spec_items(account_id, clean)
            # Recheck evidence at proposal time, even without a recent import hook.
            self._sync_component(account_id, row)
            row = self._row(account_id, cid)
            self._check_proposed_source_fields(row, clean)
            status = json.loads(row["source_status_json"])
            proposal = {"spec": clean, "base_version": row["version"] + 1, "proposed_at": now_iso(),
                        "review_fingerprints": status.get("review_fingerprints", {})}
            before = self._effective_state_json(account_id, row)
            return self._save(account_id, row, json.loads(row["spec_json"]), json.loads(before), "spec_proposal", before,
                              source_changes={"pending_spec_json": canonical_json(proposal)})

    def _check_proposed_source_fields(self, row, proposed):
        """Source scalar edits use the reversible override API, never a rule proposal."""
        current = json.loads(row["spec_json"])
        targets = set(json.loads(row["source_values_json"])) | set(json.loads(row["source_overrides_json"]))
        targets.update(b["target"] for b in json.loads(row["source_config_json"]).get("bindings", []))
        for target in targets:
            old, old_leaf = target_slot(row["kind"], current, target)
            new, new_leaf = target_slot(row["kind"], proposed, target)
            if old.get(old_leaf) != new.get(new_leaf):
                raise InvalidInput("source-bound fields must be changed with a source override")

    def accept_spec(self, account_id, cid, expected_version):
        with self._lock:
            conflict = None
            result = None
            # Hold one write transaction through the evidence check and accept.
            # Commit newly detected source changes even if acceptance is stale.
            with self.db.tx():
                self._sync_component(account_id, self._row(account_id, cid))
                try:
                    result = self._accept_spec(account_id, cid, expected_version)
                except Conflict as error:
                    conflict = error
            if conflict is not None:
                raise conflict
            return result

    def _accept_spec(self, account_id, cid, expected_version):
        with self.db.tx():
            row = self._row(account_id, cid)
            proposal = json.loads(row["pending_spec_json"]) if row["pending_spec_json"] else None
            if not proposal:
                raise InvalidInput("there is no pending component definition")
            status = json.loads(row["source_status_json"])
            if (isinstance(expected_version, bool) or not isinstance(expected_version, int)
                    or expected_version != row["version"] or proposal["base_version"] != row["version"]
                    or proposal["review_fingerprints"] != status.get("review_fingerprints", {})):
                raise Conflict("component or source changed since this proposal; review a new proposal", code="version_conflict")
            spec = self.normalize(row["kind"], proposal["spec"])
            self._validate_spec_items(account_id, spec)
            # Keep source targets meaningful after a proposed structural edit.
            self._validate_sources(account_id, row["kind"], spec, json.loads(row["source_config_json"]), row["course_id"])
            before = self._effective_state_json(account_id, row)
            state = self._clean_state(row["kind"], spec, json.loads(before))
            if row["kind"] == "checklist" and spec.get("sync_todos"):
                state = self._push_todos(account_id, spec, state, previous=json.loads(row["spec_json"]))
            status.update(needs_review=False, changed_review_sources=[], accepted_fingerprints=status.get("review_fingerprints", {}),
                          reviewed_at=now_iso())
            return self._save(account_id, row, spec, state, "accepted_spec", before,
                              source_changes={"pending_spec_json": None, "source_status_json": canonical_json(status)})

    def _validate_spec_items(self, account_id, spec):
        entries = list(spec.get("buckets", []))
        entries += [r for s in spec.get("sections", []) for r in s["rows"]]
        entries += [r for s in spec.get("categories", []) for r in s["items"]]
        for entry in entries:
            if entry.get("item_id") is not None:
                self._source_item(account_id, entry["item_id"])
