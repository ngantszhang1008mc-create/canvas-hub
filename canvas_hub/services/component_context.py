"""Small tracker reads and atomic, optimistic state patches for agents."""
import copy
import hashlib
import json

from ..util import Conflict, InvalidInput


class ComponentContext:
    @staticmethod
    def state_token(view):
        return hashlib.sha256(json.dumps([view["version"], view["state"]], sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()

    def context_view(self, account_id, cid, *, row_ids=None, include_rows=False, offset=0, limit=20):
        if row_ids is not None and (not isinstance(row_ids, list) or len(row_ids) > 30 or any(not isinstance(i, str) for i in row_ids)):
            raise InvalidInput("row_ids must contain at most 30 string IDs")
        # Keep token, version and effective linked-to-do state from one snapshot.
        with self._lock, self.db.tx():
            view = self.get(account_id, cid)
            desc = self.describe(account_id, cid)
            out = {k: desc[k] for k in ("component_id", "kind", "title", "version", "pinned", "course_id", "item_id")}
            status = view["source_status"]
            out.update(purpose=view["purpose_key"], state_token=self.state_token(view),
                       source_status={k: status[k] for k in ("availability", "needs_review", "last_success_at") if k in status},
                       computed_locally=True)
            out["source_status"]["unavailable_count"] = len(status.get("unavailable") or [])
            rows = desc.get("lines", desc.get("rows", desc.get("entries", [])))
            if view["kind"] == "grade_projector":
                rows = [{**i, "category": c["id"]} for c in view["spec"]["categories"] for i in c["items"]]
                out["totals"] = {k: v for k, v in view["computed"].items() if not isinstance(v, (list, dict))}
            else:
                out["totals"] = desc.get("totals", desc.get("summary", {}))
            out["row_count"] = len(rows)
            out["next_offset"] = None
            if include_rows or row_ids is not None:
                if row_ids is not None:
                    rows = [r for r in rows if str(r.get("id")) in row_ids]
                offset, limit = max(0, int(offset)), max(1, min(30, int(limit)))
                selected = rows[offset:offset+limit]
                out["rows"] = selected
                out["next_offset"] = offset + limit if offset + limit < len(rows) else None
                ids = {str(r.get("id")) for r in selected}
                out["state"] = {k: {i: val for i, val in v.items() if i in ids} if isinstance(v, dict) else v
                                for k, v in view["state"].items() if k not in ("entries", "links")}
            return out

    def patch_state(self, account_id, cid, patch, *, expected_version, state_token):
        if not isinstance(patch, dict) or len(json.dumps(patch)) > 32000:
            raise InvalidInput("state patch must be an object of at most 32000 characters")
        # Maps merge recursively. null deletes one key. Arrays are replaced as
        # a unit only with the exact version + effective state token the agent read.
        def merge(before, change):
            result = copy.deepcopy(before)
            for key, value in change.items():
                if value is None:
                    result.pop(key, None)
                elif isinstance(value, dict):
                    if key in result and not isinstance(result[key], dict):
                        raise InvalidInput(f"{key} is not a state map")
                    result[key] = merge(result.get(key, {}), value)
                else:
                    result[key] = copy.deepcopy(value)
            return result

        with self._lock, self.db.tx():
            view = self.get(account_id, cid)
            if type(expected_version) is not int or expected_version != view["version"] or state_token != self.state_token(view):
                raise Conflict("Tracker or linked to-do changed. Read get_component_context and review the current fields before patching.", code="version_conflict")
            if set(patch) - (set(view["state"]) - {"links"}):
                raise InvalidInput("patch contains unsupported or server-owned state fields")
            spec = view["spec"]
            row_ids = ({r["id"] for s in spec["sections"] for r in s["rows"]} if view["kind"] == "checklist" else
                       {r["id"] for r in spec["buckets"]} if view["kind"] == "score_model" else
                       {r["id"] for c in spec["categories"] for r in c["items"]} if view["kind"] == "grade_projector" else set())
            for field in ("done", "include", "values", "hypothetical", "collapsed"):
                value = patch.get(field)
                if value is None:
                    continue
                ids = {s["id"] for s in spec.get("sections", [])} if field == "collapsed" else row_ids
                if not isinstance(value, dict) or set(value) - ids:
                    raise InvalidInput(f"{field} must name current tracker IDs")
                if field in ("done", "include", "collapsed") and any(v is not None and not isinstance(v, bool) for v in value.values()):
                    raise InvalidInput(f"{field} values must be true, false or null")
            if "scenario" in patch and patch["scenario"] is not None and patch["scenario"] not in {s["id"] for s in spec.get("scenarios", [])}:
                raise InvalidInput("scenario must name a current scenario")
            if "entries" in patch and patch["entries"] is not None and (not isinstance(patch["entries"], list) or
                    any(not isinstance(entry, dict) for entry in patch["entries"])):
                raise InvalidInput("entries must be an array of entry objects")
            state = merge(view["state"], patch)
            return self.set_state(account_id, cid, state, expected_version=expected_version, base=view["state"], by="assistant")
