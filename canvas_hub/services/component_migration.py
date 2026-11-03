"""Lossless, server-owned state migration for reviewed component definitions.

Labels and array positions are never identities. An unresolved choice is shown
to the reviewer, not silently replaced by a new component's default value.
"""
from __future__ import annotations

import copy
from collections import Counter

from ..util import InvalidInput, canonical_json


def entries(kind, spec):
    groups, rows, parents = {}, {}, {}
    if kind == "score_model":
        rows = {r["id"]: r for r in spec["buckets"]}
    elif kind in ("checklist", "grade_projector"):
        group_key, row_key = ("sections", "rows") if kind == "checklist" else ("categories", "items")
        for group in spec[group_key]:
            if group["id"] in groups:
                raise InvalidInput("group IDs must be unique for a definition migration")
            groups[group["id"]] = group
            for row in group[row_key]:
                rows[row["id"]] = row
                parents[row["id"]] = group["id"]
    scenarios = {r["id"]: r for r in spec.get("scenarios", [])}
    if len(scenarios) != len(spec.get("scenarios", [])):
        raise InvalidInput("scenario IDs must be unique for a definition migration")
    return {"rows": rows, "groups": groups, "scenarios": scenarios}, parents


def plan_migration(kind, old_spec, new_spec, state, *, identity_map=None, sources=None, rebuild=False):
    """Return resolved identities, migrated state and blocking diagnostics.

    Ordinary edits may remove an unused row. Rebuilds must account for every
    existing row and keep grading rules. Neither path may drop a stored value.
    """
    explicit = identity_map if identity_map is not None else {}
    if not isinstance(explicit, dict) or set(explicit) - {"rows", "groups", "scenarios"}:
        raise InvalidInput("identity_map contains only rows, groups and scenarios maps")
    if len(canonical_json(explicit)) > 200_000:
        raise InvalidInput("identity_map is too large")
    for mapping in explicit.values():
        if (not isinstance(mapping, dict) or len(mapping) > 30000
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items())):
            raise InvalidInput("identity maps must contain old ID to new ID string pairs")
    old, old_parents = entries(kind, old_spec)
    new, new_parents = entries(kind, new_spec)
    maps, matches, unresolved = {}, [], []
    sources = sources or {}

    # A declared item ID, or one unambiguous bound item, is a stable source
    # identity. Multiple assessed lines for one source require explicit IDs.
    def source_ids(rows, use_bindings=False):
        result = {}
        for rid, row in rows.items():
            if row.get("item_id") is not None:
                result[rid] = row["item_id"]
                continue
            bound = {b["item_id"] for b in sources.get("bindings", [])
                     if len(b["target"].split("/")) > 1 and b["target"].split("/")[-2] == rid} if use_bindings else set()
            if len(bound) == 1:
                result[rid] = next(iter(bound))
        return result

    old_sources, new_sources = source_ids(old["rows"], True), source_ids(new["rows"])
    counts = Counter(old_sources.values())
    for namespace in ("rows", "groups", "scenarios"):
        mapping, methods = {}, {}
        supplied = explicit.get(namespace, {})
        for oid, nid in supplied.items():
            if oid not in old[namespace] or nid not in new[namespace]:
                unresolved.append({"type": "unknown_identity", "namespace": namespace, "old_id": oid, "new_id": nid})
        for oid in old[namespace]:
            nid, method = supplied.get(oid), "explicit"
            if oid not in supplied:
                if oid in new[namespace]:
                    nid, method = oid, "stable_id"
                elif namespace == "rows" and oid in old_sources:
                    candidates = [key for key, value in new_sources.items() if value == old_sources[oid]]
                    if len(candidates) == 1 and counts[old_sources[oid]] == 1:
                        nid, method = candidates[0], "source_identity"
                    elif candidates:
                        unresolved.append({"type": "ambiguous", "namespace": namespace, "old_id": oid,
                                           "candidates": candidates})
                elif namespace == "groups":
                    candidates = {new_parents[maps["rows"][rid]] for rid, parent in old_parents.items()
                                  if parent == oid and rid in maps["rows"]}
                    if len(candidates) == 1:
                        nid, method = next(iter(candidates)), "mapped_rows"
            if nid in new[namespace]:
                mapping[oid], methods[oid] = nid, method
        for nid, count in Counter(mapping.values()).items():
            if count > 1:
                unresolved.append({"type": "conflict", "namespace": namespace, "new_id": nid,
                                   "old_ids": [key for key, value in mapping.items() if value == nid]})
        maps[namespace] = mapping
        matches.extend({"namespace": namespace, "old_id": oid, "new_id": nid, "method": methods[oid]}
                       for oid, nid in mapping.items())
        if rebuild:
            unresolved.extend({"type": "unmapped", "namespace": namespace, "old_id": oid}
                              for oid in old[namespace] if oid not in mapping)

    for oid, nid in maps["rows"].items():
        before, after = old["rows"][oid], new["rows"][nid]
        if (oid in old_sources and nid in new_sources and old_sources[oid] != new_sources[nid]
                or kind == "score_model" and before["unit"] != after["unit"]):
            unresolved.append({"type": "incompatible", "namespace": "rows", "old_id": oid, "new_id": nid})
        collection = sources.get("collection")
        if collection and oid == f"canvas:{before.get('item_id')}":
            if oid != nid or (kind == "checklist" and new_parents[nid] != maps["groups"].get(collection["section_id"])):
                unresolved.append({"type": "collection_identity", "namespace": "rows", "old_id": oid, "new_id": nid})

    migrated, preserved = copy.deepcopy(state), 0
    for field in ("include", "values", "done", "links", "hypothetical", "collapsed"):
        if field not in state:
            continue
        namespace = "groups" if field == "collapsed" else "rows"
        migrated[field] = {}
        for oid, value in state[field].items():
            if oid not in maps[namespace]:
                unresolved.append({"type": "unmapped_state", "namespace": namespace, "old_id": oid, "field": field})
            else:
                # Membership, not truthiness: false and zero are user choices.
                migrated[field][maps[namespace][oid]] = copy.deepcopy(value)
                preserved += 1
    if state.get("scenario") is not None:
        sid = state["scenario"]
        if sid not in maps["scenarios"]:
            unresolved.append({"type": "unmapped_state", "namespace": "scenarios", "old_id": sid, "field": "scenario"})
        else:
            migrated["scenario"] = maps["scenarios"][sid]
            preserved += 1

    if rebuild:
        # A rebuild is not fresh evidence for new grading rules. Such changes
        # need a separate deliberate definition proposal and user review.
        top_fields = {"score_model": ("threshold", "unit_label"), "checklist": ("sync_todos",),
                      "grade_projector": ("scale", "target"),
                      "hours_tracker": ("target", "minimum", "points", "deadline", "unit", "source")}[kind]
        row_fields = {"score_model": ("max", "earned", "status", "gating", "min_value", "unit", "full_at", "formula", "due", "item_id"),
                      "checklist": ("points", "due", "item_id"), "grade_projector": ("max", "score", "item_id"),
                      "hours_tracker": ()}[kind]
        def compare(before, after, fields, path):
            for field in fields:
                if before.get(field) != after.get(field):
                    unresolved.append({"type": "rule_change", "path": path + field,
                                       "before": before.get(field), "after": after.get(field)})
        compare(old_spec, new_spec, top_fields, "")
        for oid, nid in maps["rows"].items():
            compare(old["rows"][oid], new["rows"][nid], row_fields, f"rows/{nid}/")
            if kind == "grade_projector" and maps["groups"].get(old_parents[oid]) != new_parents[nid]:
                unresolved.append({"type": "rule_change", "path": f"rows/{nid}/category"})
        for oid, nid in maps["groups"].items():
            if kind == "grade_projector":
                compare(old["groups"][oid], new["groups"][nid], ("weight", "drop_lowest"), f"groups/{nid}/")
        for oid, nid in maps["scenarios"].items():
            before = old["scenarios"][oid]
            expected = {field: {maps["rows"].get(rid, rid): value for rid, value in before[field].items()}
                        for field in ("include", "values")}
            compare(expected, new["scenarios"][nid], ("include", "values"), f"scenarios/{nid}/")
        for namespace in maps:
            for nid in new[namespace].keys() - set(maps[namespace].values()):
                unresolved.append({"type": "rule_change", "path": f"{namespace}/{nid}", "reason": "new_definition_entry"})

    return {"identity_map": maps, "matches": matches, "unresolved": unresolved,
            "can_accept": not unresolved, "preserved_fields": preserved, "state": migrated}


def remap_target(kind, target, identity_map, spec):
    """Translate one validated source path; return None for unresolved IDs."""
    parts = target.split("/")
    if len(parts) == 1:
        return target
    nid = identity_map["rows"].get(parts[-2])
    if nid is None:
        return None
    if kind == "score_model":
        return f"buckets/{nid}/{parts[-1]}"
    _, parents = entries(kind, spec)
    prefix, leaf = ("sections", "rows") if kind == "checklist" else ("categories", "items")
    return f"{prefix}/{parents[nid]}/{leaf}/{nid}/{parts[-1]}"
