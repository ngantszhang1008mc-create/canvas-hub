"""Account-owned placements of live components. No AI, inferred rules or snapshots.

A missing placement means library. Seeding only creates the three built-in cards
once per account. Tracker pins and checklist progress are deliberately untouched.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..util import Conflict, InvalidInput, NotFound, parse_iso

BUILTINS = {"deadlines": "Deadlines & tasks", "continue": "Continue", "attention": "Needs attention"}
MODES = ("pinned", "conditional", "library")


def checked_zone(name):
    if not isinstance(name, str) or len(name) > 100:
        raise InvalidInput("Choose a valid IANA time zone, such as Asia/Hong_Kong")
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as e:
        raise InvalidInput("Choose a valid IANA time zone, such as Asia/Hong_Kong") from e
    return name


def checked_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise InvalidInput("Dates must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise InvalidInput("Invalid calendar date") from e


class WidgetService:
    def __init__(self, hub):
        self.hub, self.db = hub, hub.db

    def _seed(self, aid):
        self.hub.identity.require_account(aid)
        with self.db.tx() as c:
            if c.execute("INSERT OR IGNORE INTO overall_widget_settings(account_id) VALUES (?)", (aid,)).rowcount:
                for pos, key in enumerate(BUILTINS):
                    c.execute("INSERT INTO overall_widgets(account_id,widget_key,mode,position) VALUES (?,?,'pinned',?)",
                              (aid, key, pos))

    def _component(self, aid, key):
        if key in BUILTINS:
            return None
        if not isinstance(key, str) or not re.fullmatch(r"component:[1-9]\d*", key):
            raise InvalidInput("Unknown widget")
        return self.hub.components.get(aid, int(key.split(":")[1]))

    def _row(self, aid, key):
        row = self.db.one("SELECT * FROM overall_widgets WHERE account_id=? AND widget_key=?", (aid, key))
        return dict(row) if row else {"widget_key": key, "mode": "library", "rule_json": None,
                                     "expanded": 0, "version": 0, "pending_json": None, "position": 1000000}

    def _item(self, aid, iid, component=None):
        if isinstance(iid, bool) or not isinstance(iid, int):
            raise InvalidInput("item_id must be an integer")
        row = self.db.one("SELECT id,course_canvas_id,item_type,title,presence,last_observed_at FROM items WHERE id=? AND account_id=?", (iid, aid))
        if not row:
            raise NotFound("Rule source not found", code="item_not_found")
        item = dict(row)
        if component and component["course_id"] and item["course_canvas_id"] != component["course_id"]:
            raise InvalidInput("The rule source must be in the component's course")
        return item

    def _rule(self, aid, rule, component):
        if not isinstance(rule, dict):
            raise InvalidInput("Conditional widgets need a structured rule")
        kind = rule.get("kind")
        fields = {"date_window": {"kind", "start", "end", "time_zone", "reason", "item_id", "max_age_days"},
                  "event_today": {"kind", "item_id", "time_zone", "max_age_days"},
                  "active_work": {"kind", "item_id", "course_id"}}
        if not isinstance(kind, str) or kind not in fields or set(rule) - fields[kind]:
            raise InvalidInput("Use date_window, event_today or active_work with their documented fields")
        out = dict(rule)
        if kind in ("date_window", "event_today"):
            out["time_zone"] = checked_zone(rule.get("time_zone"))
        if kind == "date_window":
            start, end = checked_date(rule.get("start")), checked_date(rule.get("end"))
            if end < start:
                raise InvalidInput("The date window ends before it starts")
            if not isinstance(rule.get("reason"), str) or not 1 <= len(rule["reason"].strip()) <= 300:
                raise InvalidInput("Give a short reason for this date window")
        if kind == "event_today" or "item_id" in rule:
            item = self._item(aid, rule.get("item_id"), component)
            if kind == "event_today" and item["item_type"] != "calendar_event":
                raise InvalidInput("event_today needs a stored calendar event, not an inferred deadline")
        if kind in ("date_window", "event_today") and "item_id" in out:
            age = rule.get("max_age_days", 7)
            if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 90:
                raise InvalidInput("max_age_days must be between 1 and 90")
            out["max_age_days"] = age
        elif "max_age_days" in out:
            raise InvalidInput("A freshness limit needs an item source")
        if kind == "active_work":
            if ("item_id" in rule) == ("course_id" in rule):
                raise InvalidInput("Choose exactly one item_id or course_id for active work")
            if "course_id" in rule:
                cid, _ = self.hub.components.validate_links(aid, rule["course_id"])
                if not cid or component and component["course_id"] and component["course_id"] != cid:
                    raise InvalidInput("Choose the component's course")
                out["course_id"] = cid
        return out

    def _config(self, aid, data, component, *, retained_rule=None):
        if not isinstance(data, dict) or set(data) - {"mode", "rule", "expanded"}:
            raise InvalidInput("Unknown widget configuration fields")
        mode = data.get("mode")
        if mode not in MODES:
            raise InvalidInput("Choose pinned, conditional or library")
        if not isinstance(data.get("expanded", False), bool):
            raise InvalidInput("expanded must be true or false")
        if mode == "conditional" and component is None:
            raise InvalidInput("Built-in widgets can be pinned or kept in the library")
        rule = None
        if data.get("rule") is not None:
            try:
                rule = self._rule(aid, data["rule"], component)
            except (InvalidInput, NotFound):
                # Hiding/pinning a previously accepted rule must still work after
                # its source is deleted. Only an identical retained rule qualifies;
                # new references and re-enabling conditions always revalidate.
                if mode == "conditional" or data["rule"] != retained_rule:
                    raise
                rule = retained_rule
        if mode == "conditional" and rule is None:
            raise InvalidInput("Conditional widgets need a rule")
        return {"mode": mode, "rule": rule, "expanded": data.get("expanded", False)}

    def _write(self, aid, key, component, config, previous):
        pos = previous["position"]
        if not previous["version"]:
            pos = self.db.one("SELECT COALESCE(MAX(position),-1)+1 n FROM overall_widgets WHERE account_id=?", (aid,))["n"]
        self.db.conn.execute("""INSERT INTO overall_widgets(account_id,widget_key,component_id,mode,rule_json,position,expanded,version)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account_id,widget_key) DO UPDATE SET mode=excluded.mode,
            rule_json=excluded.rule_json,expanded=excluded.expanded,version=excluded.version,pending_json=NULL""",
            (aid, key, component["id"] if component else None, config["mode"], json.dumps(config["rule"]), pos,
             int(config["expanded"]), previous["version"] + 1))
        self.db.conn.execute("UPDATE overall_widget_settings SET revision=revision+1 WHERE account_id=?", (aid,))

    @staticmethod
    def _version(row, expected):
        if isinstance(expected, bool) or not isinstance(expected, int) or row["version"] != expected:
            raise Conflict("Widget settings changed; reload before saving", code="version_conflict")

    def save(self, aid, key, config, *, expected_version):
        self._seed(aid)
        with self.db.tx():
            component = self._component(aid, key)
            row = self._row(aid, key)
            self._version(row, expected_version)
            clean = self._config(aid, config, component, retained_rule=json.loads(row["rule_json"]) if row["rule_json"] else None)
            self._write(aid, key, component, clean, row)
        return self.board(aid)

    def propose(self, aid, component_id, config):
        """A proposal is visible in Manage widgets; never changes active placement."""
        self._seed(aid)
        key = f"component:{component_id}"
        with self.db.tx():
            component = self._component(aid, key)
            clean = self._config(aid, config, component)
            row = self._row(aid, key)
            if not row["version"]:
                self._write(aid, key, component, {"mode": "library", "rule": None, "expanded": False}, row)
                row = self._row(aid, key)
            proposal = {"id": uuid4().hex, "base_version": row["version"], "config": clean}
            self.db.conn.execute("UPDATE overall_widgets SET pending_json=? WHERE account_id=? AND widget_key=?",
                                 (json.dumps(proposal), aid, key))
            self.db.conn.execute("UPDATE overall_widget_settings SET revision=revision+1 WHERE account_id=?", (aid,))
        return {"widget_key": key, "title": component["title"], "proposal": proposal,
                "message": "Review in Overall → Manage widgets. The current placement is unchanged."}

    def review(self, aid, key, *, proposal_id, approve):
        self._seed(aid)
        if not isinstance(approve, bool):
            raise InvalidInput("approve must be true or false")
        with self.db.tx():
            component = self._component(aid, key)
            row = self._row(aid, key)
            p = json.loads(row["pending_json"]) if row["pending_json"] else None
            if not p or p["id"] != proposal_id or p["base_version"] != row["version"]:
                raise Conflict("This proposal changed or is no longer current", code="version_conflict")
            if approve:
                self._write(aid, key, component, self._config(aid, p["config"], component), row)
            else:
                self.db.conn.execute("UPDATE overall_widgets SET pending_json=NULL WHERE account_id=? AND widget_key=?", (aid, key))
                self.db.conn.execute("UPDATE overall_widget_settings SET revision=revision+1 WHERE account_id=?", (aid,))
        return self.board(aid)

    def reorder(self, aid, keys, *, revision):
        self._seed(aid)
        with self.db.tx():
            current = self.db.one("SELECT revision FROM overall_widget_settings WHERE account_id=?", (aid,))["revision"]
            if isinstance(revision, bool) or not isinstance(revision, int) or revision != current:
                raise Conflict("Widget order changed; reload before reordering", code="version_conflict")
            expected = {r["widget_key"] for r in self.db.query("SELECT widget_key FROM overall_widgets WHERE account_id=?", (aid,))}
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys) or len(keys) != len(expected) or set(keys) != expected:
                raise InvalidInput("Provide every saved widget key exactly once")
            for pos, key in enumerate(keys):
                self.db.conn.execute("UPDATE overall_widgets SET position=? WHERE account_id=? AND widget_key=?", (pos, aid, key))
            self.db.conn.execute("UPDATE overall_widget_settings SET revision=revision+1 WHERE account_id=?", (aid,))
        return self.board(aid)

    def set_timezone(self, aid, name):
        self._seed(aid)
        name = checked_zone(name)
        with self.db.tx():
            self.db.conn.execute("UPDATE overall_widget_settings SET time_zone=?,revision=revision+1 WHERE account_id=?", (name, aid))
        return self.board(aid)

    def _evaluate(self, aid, rule, component, now):
        try:
            rule = self._rule(aid, rule, component)  # Recheck ownership/deletion on every read.
        except (InvalidInput, NotFound):
            return {"state": "unknown", "reason": "The rule or its source is unavailable. Review widget settings."}
        kind, item = rule["kind"], None
        source = None
        if "item_id" in rule:
            item = self._item(aid, rule["item_id"], component)
            source = {"item_id": item["id"], "title": item["title"], "observed_at": item["last_observed_at"]}
            if item["presence"] != "observed":
                return {"state": "unknown", "reason": "Source unavailable; the condition cannot be confirmed.", "source": source}
            external = getattr(self.hub, "external_sources", None)
            issue = external.item_issue(aid, item["id"]) if external else None
            if issue:
                return {"state": "unknown", "reason": issue, "source": source}
            if kind != "active_work":
                raw = self.db.one("SELECT value_json,last_observed_at FROM item_fields WHERE item_id=? AND field='start_at'", (item["id"],)) if kind == "event_today" else None
                field = {"value": json.loads(raw["value_json"]), "last_observed_at": raw["last_observed_at"]} if raw else None
                observed = field["last_observed_at"] if field else item["last_observed_at"]
                source["observed_at"] = observed
                stamp = parse_iso(observed)
                if not stamp or stamp > now + timedelta(minutes=5) or now - stamp > timedelta(days=rule["max_age_days"]):
                    return {"state": "unknown", "reason": "Source is stale; check it before relying on this reminder.", "source": source}
        if kind == "active_work":
            active = self.db.one("SELECT a.item_id,i.course_canvas_id FROM active_work a JOIN items i ON i.id=a.item_id AND i.account_id=a.account_id WHERE a.account_id=?", (aid,))
            if "course_id" in rule:
                course = self.db.one("SELECT presence FROM items WHERE account_id=? AND item_type='course' AND source_key=?", (aid, rule["course_id"]))
                if not course or course["presence"] != "observed":
                    return {"state": "unknown", "reason": "Course source unavailable."}
            visible = bool(active and (active["item_id"] == rule["item_id"] if "item_id" in rule else active["course_canvas_id"] == rule["course_id"]))
            reason = "Matches your saved active work" if visible else "Waiting for matching active work"
        else:
            zone = ZoneInfo(rule["time_zone"])
            today = now.astimezone(zone).date()
            if kind == "date_window":
                visible = checked_date(rule["start"]) <= today <= checked_date(rule["end"])
                reason = f"{rule['reason']} · {rule['start']} – {rule['end']} ({rule['time_zone']})"
            else:
                value = field["value"] if field else None
                # Never interpret a naive timestamp, unknown timezone, or freeform date as a confirmed event.
                iso = value.get("iso") if isinstance(value, dict) else None
                try:
                    start = datetime.fromisoformat(iso.replace("Z", "+00:00")) if isinstance(iso, str) and iso else None
                except (ValueError, TypeError):
                    start = None
                if not start or not start.tzinfo or value.get("precision") in ("unresolved", "date_only"):
                    return {"state": "unknown", "reason": "The event has no confirmed start time.", "source": source}
                visible = start.astimezone(zone).date() == today
                reason = f"{item['title']} · {start.astimezone(zone).strftime('%Y-%m-%d %H:%M')} ({rule['time_zone']})"
                source["start_at"] = iso
        return {"state": "matched" if visible else "waiting", "reason": reason, "source": source}

    def board(self, aid, *, now=None, component_id=None):
        self._seed(aid)
        now = now or datetime.now(timezone.utc)
        if not now.tzinfo:
            raise InvalidInput("Widget evaluation needs an aware timestamp")
        account = self.hub.identity.require_account(aid)
        settings = dict(self.db.one("SELECT * FROM overall_widget_settings WHERE account_id=?", (aid,)))
        zone = settings["time_zone"] or account["time_zone"]
        try:
            checked_zone(zone)
        except InvalidInput:
            zone = None
        components = {c["id"]: c for c in self.hub.components.list(aid, limit=500)}
        if component_id is not None:
            components[component_id] = self.hub.components.get(aid, component_id)
        # Explicitly placed older components remain present even beyond the library's page limit.
        rows = self.db.query("SELECT * FROM overall_widgets WHERE account_id=?", (aid,))
        for r in rows:
            if r["component_id"] and r["component_id"] not in components:
                components[r["component_id"]] = self.hub.components.get(aid, r["component_id"])
        keys = list(BUILTINS) + [f"component:{cid}" for cid in components]
        widgets = []
        for key in keys:
            row = self._row(aid, key)
            component = components[int(key.split(":")[1])] if key.startswith("component:") else None
            rule = json.loads(row["rule_json"]) if row["rule_json"] else None
            condition = self._evaluate(aid, rule, component, now) if rule else {"state": "none", "reason": "Pinned by you"}
            visible = row["mode"] == "pinned" or row["mode"] == "conditional" and condition["state"] == "matched"
            widgets.append({"key": key, "title": component["title"] if component else BUILTINS[key],
                            "mode": row["mode"], "rule": rule, "position": row["position"], "expanded": bool(row["expanded"]),
                            "version": row["version"], "pending": json.loads(row["pending_json"]) if row["pending_json"] else None,
                            "visible": visible, "condition": condition, "component": component})
        widgets.sort(key=lambda w: (w["position"], w["key"]))
        return {"widgets": widgets, "time_zone": zone, "revision": settings["revision"], "evaluated_at": now.isoformat(),
                "event_sources": [dict(r) for r in self.db.query("SELECT id,title,course_canvas_id,presence FROM items WHERE account_id=? AND item_type='calendar_event' ORDER BY last_observed_at DESC LIMIT 300", (aid,))]}
