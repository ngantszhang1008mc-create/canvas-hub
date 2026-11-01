"""Observation import: validation, account isolation, idempotent upserts and
change detection.

Rules implemented here (see brief sections 6 and 8):

* A field absent from an observation was *not collected*; it never erases or
  changes the stored value. ``null`` is an explicit, verified absence.
* The first time an item gets fields it produces one ``first_observed``
  event ("newly observed", not "newly published"). Events created while
  building the account's first baseline are flagged ``baseline=1``.
* A field read for the first time on an existing item is ``field_added``
  (newly collected), distinct from ``field_changed`` (an actual edit).
* Volatile fields (read state, unread counts, ...) are stored but never
  produce change events or revisions.
* Re-importing the same observation is a no-op; observations older than the
  stored value for a field never overwrite it.
* Deletion is never inferred. Only a coverage record that declares an
  exhaustive, comparable index (``absence_inference``) can mark unseen items
  ``not_in_latest_index``; a presence check of the source URL is needed for
  ``source_unavailable``. Old evidence is always kept.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from .. import dates
from ..db import Database
from ..htmltools import to_text
from ..model import compare_hash, spec_for
from ..observations import ItemObs, ItemRef, ObservationPackage, validate_package
from ..urls import canonical_url
from ..util import IdentityMismatch, InvalidInput, NotFound, canonical_json, now_iso, sha256_bytes
from .identity import IdentityService
from .jobs import JobService
from .resources import ResourceService, validate_resource_id
from .search import SearchService


def source_key_for(ref: ItemRef) -> str:
    if ref.source_key:
        return ref.source_key
    if ref.canvas_id:
        return str(ref.canvas_id)
    return canonical_url(ref.url)


class Importer:
    def __init__(self, db: Database, identity: IdentityService, jobs: JobService, search: SearchService,
                 resources: ResourceService):
        self.db = db
        self.identity = identity
        self.jobs = jobs
        self.search = search
        self.resources = resources

    # ------------------------------------------------------------------
    def validate(self, data) -> ObservationPackage:
        try:
            return validate_package(data)
        except ValidationError as e:
            errs = [{"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]} for err in e.errors()[:50]]
            raise InvalidInput("Observation package failed validation", code="invalid_package", detail=errs) from e

    def resolve_account(self, pkg: ObservationPackage, *, expected_account_id: int | None,
                        allow_new_account: bool) -> dict:
        ident = pkg.identity
        if expected_account_id is not None:
            acct = self.identity.get_account(expected_account_id)
            if acct["origin"] != ident.origin or acct["canvas_user_id"] != ident.canvas_user_id:
                raise IdentityMismatch(
                    "Package identity does not match the target account; refusing to mix account data",
                    detail={"package": {"origin": ident.origin, "canvas_user_id": ident.canvas_user_id},
                            "account": {"origin": acct["origin"], "canvas_user_id": acct["canvas_user_id"]}},
                )
            return acct
        inst = self.identity.instance_by_origin(ident.origin)
        row = None
        if inst:
            row = self.db.one("SELECT id FROM accounts WHERE instance_id=? AND canvas_user_id=?",
                              (inst["id"], ident.canvas_user_id))
        if row:
            return self.identity.get_account(row["id"])
        if not allow_new_account:
            raise NotFound(
                "No account for this package identity. Sign in to that account first, or import with allow_new_account.",
                code="unknown_account",
                detail={"origin": ident.origin, "canvas_user_id": ident.canvas_user_id},
            )
        if not inst:
            inst = self.identity.add_instance(ident.origin)
        return self.identity.upsert_verified_account(inst["id"], {
            **ident.model_dump(), "time_zone_source": ident.time_zone_source or "observation_package"})

    # ------------------------------------------------------------------
    def import_package(self, data, *, job_id: int | None = None, expected_account_id: int | None = None,
                       allow_new_account: bool = False, resource_dir: Path | None = None) -> dict:
        pkg = data if isinstance(data, ObservationPackage) else self.validate(data)
        acct = self.resolve_account(pkg, expected_account_id=expected_account_id, allow_new_account=allow_new_account)
        account_id = acct["id"]

        if job_id is None:
            prior = self.jobs.by_package(account_id, pkg.package_id)
            if prior:
                return {"status": "already_imported", "job_id": prior["id"], "account_id": account_id,
                        "stats": prior["stats"]}
            job = self.jobs.create(account_id=account_id, instance_id=acct["instance_id"], kind="import",
                                   mode=pkg.run.mode, scope=pkg.run.scope, package_id=pkg.package_id, state="running")
            job_id = job["id"]
            own_job = True
        else:
            job = self.jobs.get(job_id)
            if job["account_id"] != account_id:
                raise IdentityMismatch("Package identity does not match the job's account")
            own_job = False

        self._ensure_resources(pkg, resource_dir)

        stats = {"items_new": 0, "items_changed": 0, "items_fields_added": 0, "items_unchanged": 0,
                 "observations_duplicate": 0, "stale_fields_ignored": 0, "events": 0,
                 "marked_not_in_index": 0, "coverage_records": 0}
        try:
            with self.db.tx() as c:
                baseline = self._is_baseline(c, account_id, job_id)
                touched: list[int] = []
                for obs in pkg.items:
                    iid = self._apply_item(c, account_id, job_id, pkg.package_id, obs, baseline, stats)
                    if iid:
                        touched.append(iid)
                for pc in pkg.presence_checks:
                    self._apply_presence(c, account_id, job_id, pc, stats)
                for cov in pkg.coverage:
                    self._apply_coverage(c, account_id, job_id, pkg.run.mode, cov, stats)
                self._detect_discrepancies(c, account_id, acct.get("time_zone"), touched)
        except Exception as e:
            if own_job:
                self.jobs.set_state(job_id, "failed", error=f"{type(e).__name__}: {e}")
            raise
        self.jobs.update_stats(job_id, **stats)
        if own_job:
            self.jobs.set_state(job_id, "completed", message="Observation package imported")
        discovery = getattr(self, "after_discovery", None)
        if discovery is not None:
            discovery(account_id, pkg)
        sync = getattr(self, "after_import", None)
        if sync is not None:
            sync(account_id)
        return {"status": "imported", "job_id": job_id, "account_id": account_id, "stats": stats}

    # ------------------------------------------------------------------
    def _ensure_resources(self, pkg: ObservationPackage, resource_dir: Path | None) -> None:
        for obs in pkg.items:
            for r in obs.resources:
                if self.resources.exists(r.sha256):
                    continue
                if resource_dir is None:
                    raise InvalidInput(f"resource {r.sha256} is referenced but not available", code="missing_resource")
                p = Path(resource_dir) / validate_resource_id(r.sha256)
                if not p.is_file():
                    raise InvalidInput(f"resource {r.sha256} missing from bundle", code="missing_resource")
                data = p.read_bytes()
                if sha256_bytes(data) != r.sha256:
                    raise InvalidInput(f"resource {r.sha256} content hash mismatch", code="resource_hash_mismatch")
                self.resources.store_bytes(data)
                self.resources.extract(r.sha256, r.filename)

    def _is_baseline(self, c, account_id: int, job_id: int) -> bool:
        row = c.execute(
            "SELECT 1 FROM items WHERE account_id=? AND current_revision>0 AND COALESCE(first_observed_job_id,-1)!=? LIMIT 1",
            (account_id, job_id),
        ).fetchone()
        return row is None

    def _resolve(self, c, account_id: int, ref: ItemRef, observed_at: str, job_id: int | None) -> dict:
        key = source_key_for(ref)
        row = c.execute("SELECT * FROM items WHERE account_id=? AND item_type=? AND source_key=?",
                        (account_id, ref.type, key)).fetchone()
        if row:
            updates, params = [], []
            if ref.course_id and not row["course_canvas_id"]:
                updates.append("course_canvas_id=?")
                params.append(ref.course_id)
            if ref.canvas_id and not row["canvas_id"]:
                updates.append("canvas_id=?")
                params.append(ref.canvas_id)
            if ref.url and not row["source_url"]:
                updates.append("source_url=?")
                params.append(ref.url)
            if ref.title and not row["title"]:
                updates.append("title=?")
                params.append(ref.title)
            if updates:
                c.execute(f"UPDATE items SET {', '.join(updates)} WHERE id=?", (*params, row["id"]))
                row = c.execute("SELECT * FROM items WHERE id=?", (row["id"],)).fetchone()
            return dict(row)
        iid = c.execute(
            """INSERT INTO items(account_id, course_canvas_id, item_type, source_key, canvas_id, source_url, title,
               first_observed_at, last_observed_at, first_observed_job_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (account_id, ref.course_id, ref.type, key, ref.canvas_id, ref.url, ref.title, observed_at, observed_at, job_id),
        ).lastrowid
        return dict(c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())

    def _event(self, c, account_id, item_id, job_id, kind, observed_at, *, field=None, old=None, new=None,
               prior=None, baseline=False, stats=None):
        c.execute(
            """INSERT INTO change_events(account_id, item_id, job_id, kind, field, old_json, new_json, observed_at,
               prior_observed_at, baseline, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, item_id, job_id, kind, field,
             None if old is None else canonical_json(old), None if new is None else canonical_json(new),
             observed_at, prior, 1 if baseline else 0, now_iso()),
        )
        if stats is not None:
            stats["events"] += 1

    def _apply_item(self, c, account_id: int, job_id: int, package_id: str, obs: ItemObs, baseline: bool,
                    stats: dict) -> int | None:
        item = self._resolve(c, account_id, obs, obs.observed_at, job_id)
        iid = item["id"]
        obs_key = obs.observation_id or f"{package_id}:{obs.observed_at}"
        if c.execute("SELECT 1 FROM item_observations WHERE item_id=? AND observation_key=?", (iid, obs_key)).fetchone():
            stats["observations_duplicate"] += 1
            return None

        if not obs.fields:
            # Relationship-only observation (e.g. "this page links to X"): it
            # adds links but is not a fresh read of the item itself.
            c.execute(
                """INSERT INTO item_observations(item_id, job_id, observation_key, observed_at, method, depth, fields_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (iid, job_id, obs_key, obs.observed_at, obs.method, obs.depth, "[]"),
            )
            self._apply_links(c, account_id, job_id, iid, obs)
            return iid

        existing = {r["field"]: dict(r) for r in c.execute("SELECT * FROM item_fields WHERE item_id=?", (iid,))}
        is_first = item["current_revision"] == 0
        changed, added = [], []
        t = obs.type
        for name, value in obs.fields.items():
            h = compare_hash(t, name, value)
            vj = canonical_json(value)
            cur = existing.get(name)
            spec = spec_for(t, name)
            if cur is None:
                c.execute(
                    """INSERT INTO item_fields(item_id, field, value_json, compare_hash, first_observed_at,
                       last_observed_at, last_changed_at, method, job_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (iid, name, vj, h, obs.observed_at, obs.observed_at, obs.observed_at, obs.method, job_id),
                )
                if not spec.volatile:
                    added.append(name)
                continue
            if obs.observed_at < cur["last_observed_at"]:
                stats["stale_fields_ignored"] += 1
                continue
            if cur["compare_hash"] != h:
                c.execute(
                    """UPDATE item_fields SET value_json=?, compare_hash=?, last_observed_at=?, last_changed_at=?,
                       method=?, job_id=? WHERE item_id=? AND field=?""",
                    (vj, h, obs.observed_at, obs.observed_at, obs.method, job_id, iid, name),
                )
                if not spec.volatile:
                    changed.append(name)
                    self._event(c, account_id, iid, job_id, "field_changed", obs.observed_at, field=name,
                                old=json.loads(cur["value_json"]), new=value, prior=cur["last_observed_at"],
                                baseline=baseline, stats=stats)
            else:
                # Same meaning; keep the newest raw form (formatting-only differences).
                c.execute("UPDATE item_fields SET value_json=?, last_observed_at=?, method=?, job_id=? WHERE item_id=? AND field=?",
                          (vj, obs.observed_at, obs.method, job_id, iid, name))

        title = obs.title or obs.fields.get("title") or item["title"] or obs.fields.get("name")
        new_rev = item["current_revision"]
        if is_first and added:
            new_rev = 1
            stats["items_new"] += 1
            self._event(c, account_id, iid, job_id, "first_observed", obs.observed_at, new={"title": title},
                        baseline=baseline, stats=stats)
        elif changed or added:
            new_rev += 1
            if changed:
                stats["items_changed"] += 1
            else:
                stats["items_fields_added"] += 1
            # A field read for the first time on an item that was itself first
            # observed in this same job is part of that first observation, not news.
            same_job_item = item["first_observed_job_id"] == job_id
            for name in added:
                if not is_first and not same_job_item:
                    self._event(c, account_id, iid, job_id, "field_added", obs.observed_at, field=name,
                                new=obs.fields[name], baseline=baseline, stats=stats)
        else:
            stats["items_unchanged"] += 1

        if item["presence"] != "observed" and not is_first:
            self._event(c, account_id, iid, job_id, "reobserved", obs.observed_at,
                        old={"presence": item["presence"]}, new={"presence": "observed"}, stats=stats)

        last_obs = max(item["last_observed_at"], obs.observed_at) if item["current_revision"] else obs.observed_at
        c.execute(
            """UPDATE items SET title=?, last_observed_at=?, current_revision=?, presence='observed', presence_detail=NULL,
               presence_changed_at=CASE WHEN presence!='observed' THEN ? ELSE presence_changed_at END,
               source_url=COALESCE(?, source_url), first_observed_at=CASE WHEN current_revision=0 THEN ? ELSE first_observed_at END,
               first_observed_job_id=CASE WHEN current_revision=0 THEN ? ELSE first_observed_job_id END
               WHERE id=?""",
            (title, last_obs, new_rev, obs.observed_at, obs.url, obs.observed_at, job_id, iid),
        )

        merged = {r["field"]: json.loads(r["value_json"]) for r in
                  c.execute("SELECT field, value_json FROM item_fields WHERE item_id=?", (iid,))}
        if new_rev != item["current_revision"]:
            c.execute(
                """INSERT INTO item_revisions(item_id, revision, job_id, observed_at, source_updated_at, fields_json,
                   changed_fields_json, added_fields_json, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (iid, new_rev, job_id, obs.observed_at, obs.source_updated_at, canonical_json(merged),
                 json.dumps(changed), json.dumps(added), now_iso()),
            )
            self.search.index_item(c, account_id, iid, t, title, merged)

        c.execute(
            """INSERT INTO item_observations(item_id, job_id, observation_key, observed_at, method, depth, fields_json)
               VALUES (?,?,?,?,?,?,?)""",
            (iid, job_id, obs_key, obs.observed_at, obs.method, obs.depth, json.dumps(sorted(obs.fields))),
        )

        self._apply_links(c, account_id, job_id, iid, obs)
        self._apply_resources(c, account_id, job_id, iid, obs, new_rev, title)
        return iid

    def _apply_links(self, c, account_id: int, job_id: int, iid: int, obs: ItemObs) -> None:
        if obs.replace_links:
            keep = {(ln.relation, source_key_for(ln.to), ln.to.type, ln.label) for ln in obs.links}
            for r in c.execute(
                f"""SELECT l.id, l.relation, l.label, i.source_key, i.item_type FROM item_links l JOIN items i ON i.id=l.to_item_id
                    WHERE l.from_item_id=? AND l.relation IN ({','.join('?'*len(obs.replace_links))})""",
                (iid, *obs.replace_links),
            ).fetchall():
                if (r["relation"], r["source_key"], r["item_type"], r["label"]) not in keep:
                    c.execute("DELETE FROM item_links WHERE id=?", (r["id"],))
        for ln in obs.links:
            target = self._resolve(c, account_id, ln.to, obs.observed_at, job_id)
            c.execute(
                """INSERT INTO item_links(account_id, from_item_id, to_item_id, relation, label, position, attrs_json,
                   first_observed_at, last_observed_at) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(from_item_id, to_item_id, relation, label) DO UPDATE SET
                   position=excluded.position, attrs_json=excluded.attrs_json, last_observed_at=excluded.last_observed_at""",
                (account_id, iid, target["id"], ln.relation, ln.label or "", ln.position, canonical_json(ln.attrs),
                 obs.observed_at, obs.observed_at),
            )

    def _apply_resources(self, c, account_id: int, job_id: int, iid: int, obs: ItemObs, new_rev: int, title) -> None:
        # Originals were stored and extracted before the transaction.
        for r in obs.resources:
            had = c.execute("SELECT 1 FROM item_resources WHERE item_id=? AND resource_id=?", (iid, r.sha256)).fetchone()
            c.execute(
                """INSERT INTO item_resources(item_id, resource_id, filename, revision, observed_at, job_id) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(item_id, resource_id) DO UPDATE SET observed_at=MAX(item_resources.observed_at,excluded.observed_at)""",
                (iid, r.sha256, r.filename, new_rev, obs.observed_at, job_id),
            )
            if not had:
                # Only the newest original of an item is searchable as current text.
                for old in c.execute("SELECT id FROM search_docs WHERE item_id=? AND resource_id!='' AND resource_id!=?",
                                     (iid, r.sha256)).fetchall():
                    c.execute("DELETE FROM search_fts WHERE rowid=?", (old["id"],))
                    c.execute("DELETE FROM search_docs WHERE id=?", (old["id"],))
                text = self.resources.extracted_text_for(r.sha256)
                if text:
                    self.search.index_resource(c, account_id, iid, r.sha256, r.filename or title or "", text)

    def _apply_presence(self, c, account_id, job_id, pc, stats):
        key = source_key_for(pc.item)
        row = c.execute("SELECT * FROM items WHERE account_id=? AND item_type=? AND source_key=?",
                        (account_id, pc.item.type, key)).fetchone()
        if not row:
            return
        if pc.result in ("not_found", "forbidden"):
            if row["presence"] != "source_unavailable":
                detail = f"source check {pc.result} (HTTP {pc.http_status})" if pc.http_status else f"source check {pc.result}"
                c.execute("UPDATE items SET presence='source_unavailable', presence_detail=?, presence_changed_at=? WHERE id=?",
                          (detail, pc.checked_at, row["id"]))
                self._event(c, account_id, row["id"], job_id, "source_unavailable", pc.checked_at,
                            old={"presence": row["presence"]}, new={"presence": "source_unavailable", "detail": detail},
                            prior=row["last_observed_at"], stats=stats)
        elif pc.result == "available" and row["presence"] != "observed":
            c.execute("UPDATE items SET presence='observed', presence_detail=?, presence_changed_at=? WHERE id=?",
                      ("source still reachable but not listed in the index", pc.checked_at, row["id"]))
            self._event(c, account_id, row["id"], job_id, "reobserved", pc.checked_at,
                        old={"presence": row["presence"]}, new={"presence": "observed"}, stats=stats)

    def _apply_coverage(self, c, account_id, job_id, mode, cov, stats):
        course = cov.course_id or ""
        detail = dict(cov.detail)
        if cov.absence_inference:
            detail["absence_inference"] = cov.absence_inference.item_types
        c.execute(
            """INSERT INTO coverage(job_id, account_id, course_canvas_id, category, status, index_depth, body_depth,
               attachment_depth, expected_count, collected_count, reason, detail_json, mode, observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id, course_canvas_id, category) DO UPDATE SET status=excluded.status,
               index_depth=excluded.index_depth, body_depth=excluded.body_depth, attachment_depth=excluded.attachment_depth,
               expected_count=excluded.expected_count, collected_count=excluded.collected_count, reason=excluded.reason,
               detail_json=excluded.detail_json, observed_at=excluded.observed_at""",
            (job_id, account_id, course, cov.category, cov.status, cov.index_depth, cov.body_depth, cov.attachment_depth,
             cov.expected_count, cov.collected_count, cov.reason, canonical_json(detail), mode, cov.observed_at),
        )
        stats["coverage_records"] += 1
        if not cov.absence_inference or cov.status != "complete" or cov.index_depth != "complete":
            return
        types = cov.absence_inference.item_types
        # Account-level coverage (course_id None) spans every course, e.g. the
        # All Courses inventory for item type 'course'.
        course_clause = "course_canvas_id=?" if cov.course_id else "1=1"
        params = [account_id, *types] + ([cov.course_id] if cov.course_id else [])
        rows = c.execute(
            f"""SELECT id, presence, last_observed_at FROM items WHERE account_id=? AND item_type IN ({','.join('?'*len(types))})
                AND {course_clause} AND source_key NOT LIKE 'external:%' AND current_revision>0 AND presence='observed'
                AND NOT EXISTS (SELECT 1 FROM item_observations o WHERE o.item_id=items.id AND o.job_id=? AND o.fields_json!='[]')""",
            (*params, job_id),
        ).fetchall()
        for r in rows:
            c.execute(
                "UPDATE items SET presence='not_in_latest_index', presence_detail=?, presence_changed_at=? WHERE id=?",
                (f"not listed in the {cov.category} index during job {job_id}; source not yet rechecked", cov.observed_at, r["id"]),
            )
            self._event(c, account_id, r["id"], job_id, "not_in_latest_index", cov.observed_at,
                        old={"presence": "observed"}, new={"presence": "not_in_latest_index", "category": cov.category},
                        prior=r["last_observed_at"], stats=stats)
            stats["marked_not_in_index"] += 1

    # ------------------------------------------------------------------
    def _detect_discrepancies(self, c, account_id: int, time_zone: str | None, item_ids: list[int]) -> None:
        """Flag announcements that state a date for an assignment which differs
        from the assignment's own due date. Both sources are preserved."""
        if not item_ids:
            return
        qmarks = ",".join("?" * len(item_ids))
        ann_ids = {r["id"] for r in c.execute(
            f"SELECT id FROM items WHERE id IN ({qmarks}) AND item_type='announcement'", item_ids)}
        asg_ids = [r["id"] for r in c.execute(
            f"SELECT id FROM items WHERE id IN ({qmarks}) AND item_type='assignment'", item_ids)]
        for aid in asg_ids:
            crs = c.execute("SELECT course_canvas_id FROM items WHERE id=?", (aid,)).fetchone()["course_canvas_id"]
            ann_ids |= {r["id"] for r in c.execute(
                "SELECT id FROM items WHERE account_id=? AND item_type='announcement' AND course_canvas_id IS ?",
                (account_id, crs))}
        for ann in ann_ids:
            self._check_announcement(c, account_id, time_zone, ann)

    def _check_announcement(self, c, account_id, time_zone, ann_id):
        a = c.execute("SELECT * FROM items WHERE id=?", (ann_id,)).fetchone()
        body = c.execute("SELECT value_json FROM item_fields WHERE item_id=? AND field IN ('message','body') LIMIT 1",
                         (ann_id,)).fetchone()
        if not body:
            return
        text = to_text(json.loads(body["value_json"]) or "")
        sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
        linked = {r["to_item_id"]: (r["label"] or "") for r in c.execute(
            "SELECT to_item_id, label FROM item_links WHERE from_item_id=? AND relation='body_link'", (ann_id,))}
        assignments = c.execute(
            "SELECT id, title FROM items WHERE account_id=? AND item_type='assignment' AND course_canvas_id IS ? AND current_revision>0",
            (account_id, a["course_canvas_id"]),
        ).fetchall()
        now = now_iso()
        for asg in assignments:
            names = [n for n in {asg["title"] or "", linked.get(asg["id"], "")} if len(n.strip()) >= 6]
            if not names:
                continue
            due_row = c.execute("SELECT value_json FROM item_fields WHERE item_id=? AND field='due_at'", (asg["id"],)).fetchone()
            if not due_row:
                continue
            due = json.loads(due_row["value_json"])
            due_dt = dates.instant(due)
            if due_dt is None:
                continue
            if time_zone:
                from zoneinfo import ZoneInfo
                try:
                    due_dt = due_dt.astimezone(ZoneInfo(time_zone))
                except Exception:  # noqa: BLE001
                    pass
            mentions = []
            for s in sentences:
                low = s.lower()
                if any(n.lower() in low for n in names):
                    mentions.extend(dates.date_mentions(s))
            existing = c.execute("SELECT id FROM discrepancies WHERE item_id=? AND other_item_id=? AND kind='announced_date_differs'",
                                 (ann_id, asg["id"])).fetchone()
            if not mentions or any(m["month"] == due_dt.month and m["day"] == due_dt.day for m in mentions):
                if existing:
                    c.execute("DELETE FROM discrepancies WHERE id=?", (existing["id"],))
                continue
            detail = {"announcement_mentions": [m["text"] for m in mentions], "assignment_due": due,
                      "assignment_due_local": due_dt.strftime("%Y-%m-%d %H:%M %Z"), "time_zone": time_zone,
                      "note": "Both dates are preserved; check the source before relying on either."}
            if existing:
                c.execute("UPDATE discrepancies SET detail_json=?, last_detected_at=? WHERE id=?",
                          (canonical_json(detail), now, existing["id"]))
            else:
                c.execute(
                    """INSERT INTO discrepancies(account_id, item_id, other_item_id, kind, detail_json, first_detected_at,
                       last_detected_at) VALUES (?,?,?,?,?,?,?)""",
                    (account_id, ann_id, asg["id"], "announced_date_differs", canonical_json(detail), now, now),
                )

