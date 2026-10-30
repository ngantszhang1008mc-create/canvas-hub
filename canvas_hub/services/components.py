"""Interactive components with saved state (score models, checklists).

The assistant defines a component (the *spec*); the user changes its *state*
in the page (ticks, skipped lines, partial values). Both are stored, every
change keeps the previous version for undo, and the assistant can read the
state back with the computed totals. Components can be pinned to the
Trackers page.

Checklists can be linked to local to-dos: ticking a row marks its to-do done
and a to-do finished elsewhere shows as ticked here. Nothing is sent to Canvas.
"""

from __future__ import annotations

import json
import re
import threading

from .. import projections, scoring
from ..db import Database
from ..util import Conflict, InvalidInput, NotFound, now_iso, parse_iso
from .annotations import AnnotationService

KINDS = ("score_model", "checklist", "grade_projector", "hours_tracker")
MAX_ROWS = 300
MAX_SPEC = 200_000
KEEP_VERSIONS = 50
_ID = re.compile(r"^[\w.:-]{1,64}$")


def normalize_checklist(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise InvalidInput("the checklist must be an object")
    sections = spec.get("sections")
    if sections is None and isinstance(spec.get("rows"), list):
        sections = [{"id": "s1", "title": "", "rows": spec["rows"]}]
    if not isinstance(sections, list) or not sections:
        raise InvalidInput("sections must be a non-empty list")
    out, seen, n = [], set(), 0
    for si, s in enumerate(sections):
        if not isinstance(s, dict) or not isinstance(s.get("rows"), list):
            raise InvalidInput(f"section {si + 1} needs rows")
        sid = str(s.get("id") or f"s{si + 1}")
        rows = []
        for ri, r in enumerate(s["rows"]):
            if not isinstance(r, dict) or not r.get("label"):
                raise InvalidInput(f"row {ri + 1} in section {sid} needs a label")
            rid = str(r.get("id") or f"{sid}.{ri + 1}")
            if not _ID.match(rid) or rid in seen:
                raise InvalidInput(f"row id {rid!r} must be unique (letters, digits, _ . : -)")
            seen.add(rid)
            n += 1
            row = {"id": rid, "label": str(r["label"])[:300]}
            if r.get("points") is not None:
                row["points"] = scoring._num(r["points"], f"{rid}: points")
            if r.get("due"):
                if not parse_iso(str(r["due"])) and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(r["due"])):
                    raise InvalidInput(f"{rid}: due must be an ISO date or timestamp")
                row["due"] = str(r["due"])
            if r.get("item_id") is not None:
                row["item_id"] = int(r["item_id"])
            if r.get("note"):
                row["note"] = str(r["note"])[:500]
            if r.get("done"):
                row["done"] = True
            rows.append(row)
        out.append({"id": sid, "title": str(s.get("title") or "")[:200], "rows": rows})
    if n > MAX_ROWS:
        raise InvalidInput(f"at most {MAX_ROWS} rows")
    return {"title": str(spec.get("title") or "Checklist")[:200], "sections": out,
            "sync_todos": bool(spec.get("sync_todos"))}


def checklist_totals(spec: dict, state: dict) -> dict:
    done = state.get("done") or {}
    secs, all_pts, all_done_pts, cnt, dcnt = [], 0.0, 0.0, 0, 0
    for s in spec["sections"]:
        pts = sum(r.get("points") or 0 for r in s["rows"])
        dpts = sum(r.get("points") or 0 for r in s["rows"] if done.get(r["id"]))
        dc = sum(1 for r in s["rows"] if done.get(r["id"]))
        secs.append({"id": s["id"], "title": s["title"], "points": pts, "done_points": dpts,
                     "count": len(s["rows"]), "done_count": dc})
        all_pts, all_done_pts, cnt, dcnt = all_pts + pts, all_done_pts + dpts, cnt + len(s["rows"]), dcnt + dc
    open_rows = [r for s in spec["sections"] for r in s["rows"] if not done.get(r["id"])]
    nxt = sorted((r for r in open_rows if r.get("due")), key=lambda r: r["due"])
    return {"sections": secs, "points": all_pts, "done_points": all_done_pts, "count": cnt, "done_count": dcnt,
            "open_count": cnt - dcnt, "next_due": nxt[0] if nxt else None}


class ComponentService:
    def __init__(self, db: Database, annotations: AnnotationService):
        self.db = db
        self.annotations = annotations
        self._lock = threading.RLock()     # one change at a time (with linked to-dos it is read-modify-write)

    # ---- validation -------------------------------------------------------------
    @staticmethod
    def normalize(kind: str, spec: dict) -> dict:
        if kind not in KINDS:
            raise InvalidInput(f"kind must be one of {list(KINDS)}")
        if len(json.dumps(spec, default=str)) > MAX_SPEC:
            raise InvalidInput("the component definition is too large")
        return {"score_model": scoring.normalize_model, "checklist": normalize_checklist,
                "grade_projector": projections.normalize_projector, "hours_tracker": projections.normalize_hours}[kind](spec)

    @staticmethod
    def _clean_state(kind: str, spec: dict, state: dict) -> dict:
        if not isinstance(state, dict):
            raise InvalidInput("state must be an object")
        if kind == "score_model":
            return scoring.normalize_state(spec, state)
        if kind == "hours_tracker":
            return projections.clean_hours_state(state)
        if kind == "grade_projector":
            ids = {it["id"] for c in spec["categories"] for it in c["items"]}
            return {"hypothetical": {k: scoring._num(v, f"score for {k}") for k, v in (state.get("hypothetical") or {}).items()
                                     if k in ids and v is not None and v != ""}}
        ids = {r["id"] for s in spec["sections"] for r in s["rows"]}
        sids = {s["id"] for s in spec["sections"]}
        return {"done": {k: True for k, v in (state.get("done") or {}).items() if k in ids and v},
                "links": {k: int(v) for k, v in (state.get("links") or {}).items() if k in ids and v},
                "collapsed": {k: True for k, v in (state.get("collapsed") or {}).items() if k in sids and v}}

    # ---- read ------------------------------------------------------------------------
    def _row(self, account_id: int, cid: int) -> dict:
        row = self.db.one("SELECT * FROM components WHERE id=? AND account_id=?", (int(cid), account_id))
        if not row:
            raise NotFound("component not found", code="component_not_found")
        return dict(row)

    def _view(self, account_id: int, row: dict) -> dict:
        spec, state = json.loads(row["spec_json"]), json.loads(row["state_json"])
        if row["kind"] == "checklist" and spec.get("sync_todos") and state.get("links"):
            state = self._pull_todos(account_id, spec, state)
        out = {"id": row["id"], "kind": row["kind"], "title": row["title"], "spec": spec, "state": state,
               "version": row["version"], "pinned": bool(row["pinned"]), "conversation_id": row["conversation_id"],
               "created_at": row["created_at"], "updated_at": row["updated_at"],
               "can_undo": bool(self.db.one("SELECT 1 FROM component_versions WHERE component_id=?", (row["id"],)))}
        if row["kind"] == "score_model":
            out["computed"] = scoring.evaluate(spec, state)
        elif row["kind"] == "grade_projector":
            out["computed"] = projections.project(spec, state)
        elif row["kind"] == "hours_tracker":
            out["computed"] = projections.hours_summary(spec, state)
        else:
            out["computed"] = checklist_totals(spec, state)
        return out

    def get(self, account_id: int, cid: int) -> dict:
        return self._view(account_id, self._row(account_id, cid))

    def list(self, account_id: int, *, pinned: bool | None = None, limit: int = 100) -> list[dict]:
        where, params = "account_id=?", [account_id]
        if pinned is not None:
            where += " AND pinned=?"
            params.append(1 if pinned else 0)
        rows = self.db.query(f"SELECT * FROM components WHERE {where} ORDER BY updated_at DESC LIMIT ?",
                             (*params, max(1, min(int(limit), 500))))
        return [self._view(account_id, dict(r)) for r in rows]

    # ---- write -------------------------------------------------------------------------
    def create(self, account_id: int, kind: str, spec: dict, *, conversation_id: int | None = None,
               created_by: str = "assistant", state: dict | None = None) -> dict:
        clean = self.normalize(kind, spec)
        if kind == "checklist":
            initial = {"done": {r["id"]: True for s in clean["sections"] for r in s["rows"] if r.get("done")}}
        else:
            initial = {}
        st = self._clean_state(kind, clean, {**initial, **(state or {})})
        if kind == "checklist" and clean.get("sync_todos"):
            st = self._push_todos(account_id, clean, st)     # linked from the start, not only after the first tick
        now = now_iso()
        with self.db.tx() as c:
            cid = c.execute(
                """INSERT INTO components(account_id, conversation_id, kind, title, spec_json, state_json, created_by,
                   created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (account_id, conversation_id, kind, clean["title"], json.dumps(clean), json.dumps(st), created_by, now, now),
            ).lastrowid
        return self.get(account_id, cid)

    def _effective_state_json(self, account_id: int, row: dict) -> str:
        """The state as the user sees it (linked to-dos finished elsewhere count as ticked): what undo goes back to.
        Taken before a change pushes new to-do states."""
        spec, state = json.loads(row["spec_json"]), json.loads(row["state_json"])
        if row["kind"] == "checklist" and spec.get("sync_todos") and state.get("links"):
            return json.dumps(self._pull_todos(account_id, spec, state))
        return row["state_json"]

    def _save(self, account_id: int, row: dict, spec: dict, state: dict, by: str, history_state: str | None = None) -> dict:
        now = now_iso()
        with self.db.tx() as c:
            # Conditional on the version that was read: a concurrent writer makes this a conflict, never an overwrite.
            n = c.execute("""UPDATE components SET spec_json=?, state_json=?, title=?, version=version+1, updated_at=?
                             WHERE id=? AND version=?""",
                          (json.dumps(spec), json.dumps(state), spec["title"], now, row["id"], row["version"])).rowcount
            if n != 1:
                raise Conflict("this component changed elsewhere; reload it", code="version_conflict")
            c.execute("""INSERT OR REPLACE INTO component_versions(component_id, version, spec_json, state_json,
                         changed_by, created_at) VALUES (?,?,?,?,?,?)""",
                      (row["id"], row["version"], row["spec_json"], history_state or row["state_json"], by, now))
            c.execute("""DELETE FROM component_versions WHERE component_id=? AND version NOT IN
                         (SELECT version FROM component_versions WHERE component_id=? ORDER BY version DESC LIMIT ?)""",
                      (row["id"], row["id"], KEEP_VERSIONS))
        return self.get(account_id, row["id"])

    def set_state(self, account_id: int, cid: int, state: dict, *, expected_version: int | None = None,
                  base: dict | None = None, by: str = "user") -> dict:
        """``base`` is the state the caller last saw. For a checklist linked to to-dos, rows the caller did not
        change since then keep their to-do's current state, so a page that loaded before a to-do was finished
        elsewhere cannot reopen it by saving an unrelated row."""
        with self._lock:
            return self._set_state(account_id, cid, state, expected_version=expected_version, base=base, by=by)

    def _set_state(self, account_id: int, cid: int, state: dict, *, expected_version: int | None, by: str,
                   base: dict | None = None) -> dict:
        row = self._row(account_id, cid)
        if expected_version is not None and int(expected_version) != row["version"]:
            raise Conflict("this component changed elsewhere; reload it", code="version_conflict")
        spec = json.loads(row["spec_json"])
        before = self._effective_state_json(account_id, row)
        st = self._clean_state(row["kind"], spec, state)
        if row["kind"] == "checklist" and spec.get("sync_todos") and isinstance(base, dict):
            seen = self._clean_state("checklist", spec, base)["done"]
            current = json.loads(before)["done"]                    # ticks as they are now, to-dos included
            ids = {r["id"] for s in spec["sections"] for r in s["rows"]}
            # a row the caller changed takes the caller's tick; any other row keeps its current one
            st["done"] = {i: True for i in ids if (st["done"].get(i, False) if st["done"].get(i, False) != seen.get(i, False)
                                                   else current.get(i, False))}
        if row["kind"] == "checklist" and spec.get("sync_todos"):
            # Links are server-owned: a client that sends stale or missing links must not create duplicate to-dos.
            saved = json.loads(row["state_json"]).get("links") or {}
            st["links"] = {**st["links"], **saved}
            st = self._push_todos(account_id, spec, st)
        return self._save(account_id, row, spec, st, by, before)

    def update_spec(self, account_id: int, cid: int, spec: dict, *, by: str = "assistant") -> dict:
        with self._lock:
            row = self._row(account_id, cid)
            clean = self.normalize(row["kind"], spec)
            before = self._effective_state_json(account_id, row)
            st = self._clean_state(row["kind"], clean, json.loads(before))   # ticks as they are now, e.g. when sync turns off
            if row["kind"] == "checklist" and clean.get("sync_todos"):
                st = self._push_todos(account_id, clean, st, previous=json.loads(row["spec_json"]))
            return self._save(account_id, row, clean, st, by, before)

    def undo(self, account_id: int, cid: int) -> dict:
        with self._lock:
            row = self._row(account_id, cid)
            prev = self.db.one("""SELECT * FROM component_versions WHERE component_id=? ORDER BY version DESC LIMIT 1""",
                               (row["id"],))
            if not prev:
                raise InvalidInput("nothing to undo", code="nothing_to_undo")
            spec = json.loads(prev["spec_json"])
            state_json = prev["state_json"]
            if row["kind"] == "checklist" and spec.get("sync_todos"):
                # Keep the to-dos created since (no orphans or duplicates) and put their states back too.
                st = json.loads(state_json)
                st["links"] = {**(st.get("links") or {}), **(json.loads(row["state_json"]).get("links") or {})}
                st = self._push_todos(account_id, spec, self._clean_state("checklist", spec, st), previous=json.loads(row["spec_json"]))
                state_json = json.dumps(st)
            with self.db.tx() as c:
                n = c.execute("""UPDATE components SET spec_json=?, state_json=?, title=?, version=version+1, updated_at=?
                                 WHERE id=? AND version=?""",
                              (prev["spec_json"], state_json, spec["title"], now_iso(), row["id"], row["version"])).rowcount
                if n != 1:
                    raise Conflict("this component changed elsewhere; reload it", code="version_conflict")
                c.execute("DELETE FROM component_versions WHERE component_id=? AND version=?", (row["id"], prev["version"]))
            return self.get(account_id, row["id"])

    def pin(self, account_id: int, cid: int, pinned: bool) -> dict:
        row = self._row(account_id, cid)
        with self.db.tx() as c:
            c.execute("UPDATE components SET pinned=?, updated_at=? WHERE id=?", (1 if pinned else 0, now_iso(), row["id"]))
        return self.get(account_id, row["id"])

    def delete(self, account_id: int, cid: int) -> dict:
        row = self._row(account_id, cid)
        with self.db.tx() as c:
            c.execute("DELETE FROM components WHERE id=?", (row["id"],))
        return {"deleted": row["id"]}

    # ---- to-do sync -------------------------------------------------------------------
    @staticmethod
    def _todo_due(r: dict) -> str | None:
        due = r.get("due")
        return None if due and len(due) == 10 else due    # date-only dues stay on the row; to-dos need an exact time

    def _push_todos(self, account_id: int, spec: dict, st: dict, previous: dict | None = None) -> dict:
        """Create or update the local to-do behind each row so both stay in step. With ``previous`` (the spec
        being replaced), a row whose label or due date changed passes that change on to its to-do; rows that did
        not change leave the to-do's own title and time alone."""
        links = dict(st.get("links") or {})
        before = {r["id"]: r for s in (previous or {}).get("sections", []) for r in s["rows"]}
        for s in spec["sections"]:
            for r in s["rows"]:
                ticked = bool(st["done"].get(r["id"]))
                want = "done" if ticked else "open"
                aid = links.get(r["id"])
                if aid:
                    try:
                        cur = self.annotations.get(account_id, aid)
                    except NotFound:
                        aid = None
                    else:
                        # Unticked keeps the to-do's own "in progress" / "blocked"; only done <-> not done is synced.
                        in_step = cur["task_state"] == want or (not ticked and cur["task_state"] != "done")
                        changes = {} if in_step else {"task_state": want}
                        old = before.get(r["id"])
                        if old is not None and old["label"] != r["label"]:
                            changes["title"] = r["label"]
                        if old is not None and self._todo_due(old) != self._todo_due(r):
                            changes["local_due"] = self._todo_due(r)
                        if changes:
                            self.annotations.update(account_id, aid, **changes)
                        continue
                due = self._todo_due(r)
                try:
                    a = self.annotations.create(account_id, kind="task", title=r["label"], item_id=r.get("item_id"),
                                                local_due=due, task_state=want)
                except NotFound:        # the linked item is gone; keep the to-do without it
                    a = self.annotations.create(account_id, kind="task", title=r["label"], local_due=due, task_state=want)
                links[r["id"]] = a["id"]
        return {**st, "links": links}

    def _pull_todos(self, account_id: int, spec: dict, st: dict) -> dict:
        done = dict(st.get("done") or {})
        links = dict(st["links"])
        for rid, aid in list(links.items()):
            try:
                a = self.annotations.get(account_id, aid)
            except NotFound:
                links.pop(rid)
                continue
            if a["task_state"] == "done":
                done[rid] = True
            else:
                done.pop(rid, None)
        return {**st, "done": done, "links": links}

    # ---- for the assistant -------------------------------------------------------------
    def describe(self, account_id: int, cid: int) -> dict:
        """Compact read-back: what is ticked/skipped and the totals."""
        v = self.get(account_id, cid)
        out = {"component_id": v["id"], "kind": v["kind"], "title": v["title"], "version": v["version"],
               "pinned": v["pinned"], "updated_at": v["updated_at"]}
        if v["kind"] == "score_model":
            comp = v["computed"]
            out["totals"] = comp["totals"]
            out["lines"] = [{k: x[k] for k in ("id", "label", "status", "max", "points", "lost", "included", "note")
                             if x.get(k) is not None} | ({"value": x["value"]} if "value" in x else {})
                            for x in comp["lines"]]
            out["scenario"] = comp["state"]["scenario"]
            out["alerts"] = comp["alerts"]
        elif v["kind"] == "grade_projector":
            out["projection"] = v["computed"]
            out["hypothetical"] = v["state"].get("hypothetical") or {}
        elif v["kind"] == "hours_tracker":
            out["summary"] = v["computed"]
            out["entries"] = v["state"].get("entries") or []
        else:
            done = v["state"].get("done") or {}
            out["totals"] = v["computed"]
            out["rows"] = [{"id": r["id"], "label": r["label"], "section": s["title"], "done": bool(done.get(r["id"])),
                            **({"points": r["points"]} if "points" in r else {}), **({"due": r["due"]} if "due" in r else {})}
                           for s in v["spec"]["sections"] for r in s["rows"]]
        return out
