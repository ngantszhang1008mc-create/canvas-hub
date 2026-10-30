"""Small per-account settings (JSON values) with defaults.

Keys in use:
- ``canvas_writes``  {"enabled": false, "kinds": [...]}  - confirmed writes to Canvas (off by default)
- ``auto_refresh``   {"minutes": 0}                      - background quick checks while the app runs (0 = off)
- ``notifications``  {"last_seen_change_id": 0}         - what the change bell has shown
"""

from __future__ import annotations

import json

from ..db import Database
from ..util import now_iso

DEFAULTS = {
    "canvas_writes": {"enabled": False, "kinds": ["submission_text", "submission_file", "discussion_reply", "message"]},
    "auto_refresh": {"minutes": 0},
    "notifications": {"last_seen_change_id": 0},
}


class AccountSettings:
    def __init__(self, db: Database):
        self.db = db

    def get(self, account_id: int, key: str) -> dict:
        row = self.db.one("SELECT value_json FROM account_settings WHERE account_id=? AND key=?", (account_id, key))
        base = json.loads(json.dumps(DEFAULTS.get(key, {})))
        if row:
            base.update(json.loads(row["value_json"]))
        return base

    def set(self, account_id: int, key: str, value: dict) -> dict:
        merged = {**self.get(account_id, key), **value}
        with self.db.tx() as c:
            c.execute("""INSERT INTO account_settings(account_id, key, value_json, updated_at) VALUES (?,?,?,?)
                         ON CONFLICT(account_id, key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                      (account_id, key, json.dumps(merged), now_iso()))
        return merged
