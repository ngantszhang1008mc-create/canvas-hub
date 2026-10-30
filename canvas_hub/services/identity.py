"""Canvas instances, verified accounts and collection scope."""

from __future__ import annotations

import json
from typing import Any

from ..db import Database
from ..model import CATEGORIES, DEFAULT_CATEGORIES
from ..urls import parse_canvas_address
from ..util import InvalidInput, NotFound, now_iso

DEFAULT_SCOPE: dict[str, Any] = {
    "courses": "current",                 # "current" | "all" | list of course ids
    "categories": list(DEFAULT_CATEGORIES),
    "calendar_days_back": 14,
    "calendar_days_ahead": 120,
    "download_files": True,
    "max_file_mb": 100,
    "discussion_replies": "staff_only",   # staff_only | counts_only
    "allow_view_completion": False,       # opening "must view" module items advances progress
}


def normalize_scope(raw: dict | None, base: dict | None = None) -> dict:
    scope = dict(DEFAULT_SCOPE)
    if base:
        scope.update({k: v for k, v in base.items() if k in DEFAULT_SCOPE})
    if not raw:
        return scope
    unknown = set(raw) - set(DEFAULT_SCOPE)
    if unknown:
        raise InvalidInput(f"Unknown scope keys: {sorted(unknown)}")
    scope.update(raw)
    c = scope["courses"]
    if isinstance(c, list):
        if not all(isinstance(x, (str, int)) and str(x).isdigit() for x in c):
            raise InvalidInput("scope.courses list must contain Canvas course ids")
        scope["courses"] = [str(x) for x in c]
    elif c not in ("current", "all"):
        raise InvalidInput("scope.courses must be 'current', 'all' or a list of course ids")
    cats = scope["categories"]
    if not isinstance(cats, list) or not all(x in CATEGORIES for x in cats):
        raise InvalidInput(f"scope.categories must be a subset of {sorted(CATEGORIES)}")
    if "courses" not in cats:
        cats = ["courses", *cats]
    scope["categories"] = [c for c in CATEGORIES if c in cats]
    for k in ("calendar_days_back", "calendar_days_ahead", "max_file_mb"):
        v = scope[k]
        if not isinstance(v, int) or v < 0 or v > 3650:
            raise InvalidInput(f"scope.{k} must be an integer between 0 and 3650")
    if scope["discussion_replies"] not in ("staff_only", "counts_only"):
        raise InvalidInput("scope.discussion_replies must be 'staff_only' or 'counts_only'")
    for k in ("download_files", "allow_view_completion"):
        if not isinstance(scope[k], bool):
            raise InvalidInput(f"scope.{k} must be true or false")
    return scope


class IdentityService:
    def __init__(self, db: Database):
        self.db = db

    # ---- instances -------------------------------------------------------
    def add_instance(self, address: str, label: str | None = None) -> dict:
        addr = parse_canvas_address(address)
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM instances WHERE origin=?", (addr.origin,)).fetchone()
            if row:
                c.execute("UPDATE instances SET entry_url=?, label=COALESCE(?, label) WHERE id=?",
                          (addr.entry_url, label, row["id"]))
                iid = row["id"]
            else:
                iid = c.execute(
                    "INSERT INTO instances(origin, label, entry_url, created_at) VALUES (?,?,?,?)",
                    (addr.origin, label or addr.origin.split("://", 1)[1], addr.entry_url, now_iso()),
                ).lastrowid
        return self.get_instance(iid)

    def get_instance(self, instance_id: int) -> dict:
        row = self.db.one("SELECT * FROM instances WHERE id=?", (instance_id,))
        if not row:
            raise NotFound(f"instance {instance_id} not found")
        return dict(row)

    def instance_by_origin(self, origin: str) -> dict | None:
        row = self.db.one("SELECT * FROM instances WHERE origin=?", (origin,))
        return dict(row) if row else None

    def list_instances(self) -> list[dict]:
        out = []
        for r in self.db.query("SELECT * FROM instances ORDER BY id"):
            d = dict(r)
            d["accounts"] = self.list_accounts(r["id"])
            out.append(d)
        return out

    # ---- accounts --------------------------------------------------------
    def upsert_verified_account(self, instance_id: int, ident: dict) -> dict:
        uid = str(ident["canvas_user_id"])
        now = now_iso()
        with self.db.tx() as c:
            row = c.execute("SELECT id FROM accounts WHERE instance_id=? AND canvas_user_id=?",
                            (instance_id, uid)).fetchone()
            vals = (ident.get("display_name"), ident.get("login_id"), ident.get("primary_email"),
                    ident.get("time_zone"), ident.get("time_zone_source"), ident.get("locale"))
            if row:
                c.execute(
                    """UPDATE accounts SET display_name=COALESCE(?,display_name), login_id=COALESCE(?,login_id),
                       primary_email=COALESCE(?,primary_email), time_zone=COALESCE(?,time_zone),
                       time_zone_source=COALESCE(?,time_zone_source), locale=COALESCE(?,locale),
                       last_verified_at=? WHERE id=?""",
                    (*vals, now, row["id"]),
                )
                aid = row["id"]
            else:
                aid = c.execute(
                    """INSERT INTO accounts(instance_id, canvas_user_id, display_name, login_id, primary_email,
                       time_zone, time_zone_source, locale, first_verified_at, last_verified_at, scope_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (instance_id, uid, *vals, now, now, json.dumps(DEFAULT_SCOPE)),
                ).lastrowid
        return self.get_account(aid)

    def get_account(self, account_id: int) -> dict:
        row = self.db.one(
            "SELECT a.*, i.origin, i.label AS instance_label FROM accounts a JOIN instances i ON i.id=a.instance_id WHERE a.id=?",
            (account_id,),
        )
        if not row:
            raise NotFound(f"account {account_id} not found", code="account_not_found")
        d = dict(row)
        d["scope"] = normalize_scope(json.loads(d.pop("scope_json") or "{}"))
        return d

    def list_accounts(self, instance_id: int | None = None) -> list[dict]:
        sql = "SELECT id FROM accounts" + (" WHERE instance_id=?" if instance_id else "") + " ORDER BY id"
        return [self.get_account(r["id"]) for r in self.db.query(sql, (instance_id,) if instance_id else ())]

    def set_scope(self, account_id: int, scope: dict) -> dict:
        acct = self.get_account(account_id)
        merged = normalize_scope(scope, acct["scope"])
        with self.db.tx() as c:
            c.execute("UPDATE accounts SET scope_json=? WHERE id=?", (json.dumps(merged), account_id))
        return merged

    def require_account(self, account_id) -> dict:
        try:
            aid = int(account_id)
        except (TypeError, ValueError) as e:
            raise InvalidInput("account_id must be an integer") from e
        return self.get_account(aid)
