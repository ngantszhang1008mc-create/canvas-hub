"""Local grading rules per course: what Canvas data leaves out.

Canvas often reports 0 for every assignment-group weight, has no notion of
"zero until 10 hours" or all-or-nothing lines, and shows some score rules
only as images. The user (or the assistant, from the syllabus) records them
here; they stay on this computer and are never sent to Canvas.

Shape (``account_settings`` key ``grading:<course id>``)::

    {"groups": {"<assignment group name>": {"weight": 30, "drop_lowest": 1}},
     "items":  {"<assignment canvas id>": {"unit": "hours", "full_at": 20, "gating": "min_threshold",
                                           "min_value": 10, "formula": "...", "exclude": false}},
     "threshold": 80, "scale": [{"letter": "A", "min": 85}, ...], "source": "syllabus p.2"}

``course_score_lines`` and the grade projector apply them on top of Canvas.
"""

from __future__ import annotations

import json
import re

from .. import formula as formula_lang
from ..db import Database
from ..scoring import FORMULA_NAMES, GATING, UNITS, _num
from ..util import InvalidInput, now_iso

_KEY = "grading:{}"
_CID = re.compile(r"^[\w.:-]{1,64}$")


def normalize_rules(rules: dict) -> dict:
    if not isinstance(rules, dict):
        raise InvalidInput("grading rules must be an object")
    groups = {}
    for name, g in list((rules.get("groups") or {}).items())[:60]:
        if not isinstance(g, dict):
            raise InvalidInput(f"group {name!r} must be an object")
        out = {}
        if g.get("weight") is not None:
            out["weight"] = _num(g["weight"], f"{name}: weight")
            if out["weight"] > 100:
                raise InvalidInput(f"{name}: weight is a percentage (0 to 100)")
        if g.get("drop_lowest"):
            out["drop_lowest"] = int(_num(g["drop_lowest"], f"{name}: drop_lowest"))
        groups[str(name)[:120]] = out
    items = {}
    for cid, it in list((rules.get("items") or {}).items())[:400]:
        cid = str(cid)
        if not _CID.match(cid) or not isinstance(it, dict):
            raise InvalidInput(f"item rules are keyed by the assignment's Canvas id (got {cid!r})")
        out = {}
        if it.get("unit") is not None:
            if it["unit"] not in UNITS:
                raise InvalidInput(f"{cid}: unit must be points or hours")
            out["unit"] = it["unit"]
        if it.get("gating") is not None:
            if it["gating"] not in GATING:
                raise InvalidInput(f"{cid}: gating must be one of {list(GATING)}")
            out["gating"] = it["gating"]
        for k in ("full_at", "min_value"):
            if it.get(k) is not None:
                out[k] = _num(it[k], f"{cid}: {k}")
        if out.get("gating") == "min_threshold" and out.get("min_value") is None:
            raise InvalidInput(f"{cid}: min_threshold gating needs min_value")
        if it.get("formula"):
            unknown = formula_lang.names(formula_lang.parse(str(it["formula"]))) - FORMULA_NAMES
            if unknown:
                raise InvalidInput(f"{cid}: formula can use {sorted(FORMULA_NAMES)}, not {sorted(unknown)}")
            out["formula"] = str(it["formula"])
        if it.get("exclude"):
            out["exclude"] = True
        if it.get("note"):
            out["note"] = str(it["note"])[:300]
        items[cid] = out
    clean = {"groups": groups, "items": items}
    if rules.get("threshold") is not None:
        clean["threshold"] = _num(rules["threshold"], "threshold")
    if rules.get("scale"):
        scale = rules["scale"]
        if not isinstance(scale, list) or not all(isinstance(x, dict) and "letter" in x and "min" in x for x in scale):
            raise InvalidInput("scale must be a list of {letter, min}")
        clean["scale"] = [{"letter": str(x["letter"])[:4], "min": _num(x["min"], "scale min")} for x in scale][:20]
    if rules.get("source"):
        clean["source"] = str(rules["source"])[:300]
    return clean


class GradingRules:
    def __init__(self, db: Database):
        self.db = db

    def get(self, account_id: int, course_id: str) -> dict:
        row = self.db.one("SELECT value_json, updated_at FROM account_settings WHERE account_id=? AND key=?",
                          (account_id, _KEY.format(course_id)))
        if not row:
            return {"groups": {}, "items": {}, "updated_at": None}
        return {**json.loads(row["value_json"]), "updated_at": row["updated_at"]}

    def set(self, account_id: int, course_id: str, rules: dict) -> dict:
        """Replace the course's rules (send the whole object; an empty one clears them)."""
        clean = normalize_rules(rules)
        with self.db.tx() as c:
            c.execute("""INSERT INTO account_settings(account_id, key, value_json, updated_at) VALUES (?,?,?,?)
                         ON CONFLICT(account_id, key) DO UPDATE SET value_json=excluded.value_json,
                         updated_at=excluded.updated_at""",
                      (account_id, _KEY.format(course_id), json.dumps(clean), now_iso()))
        return self.get(account_id, course_id)

    @staticmethod
    def apply_to_line(line: dict, canvas_id: str, rules: dict) -> dict | None:
        """A drafted score line with the item rules applied; None when the rules leave it out."""
        it = (rules.get("items") or {}).get(str(canvas_id)) or {}
        if it.get("exclude"):
            return None
        out = {**line, **{k: v for k, v in it.items() if k in ("unit", "full_at", "gating", "min_value", "formula")}}
        if it.get("note"):
            out["note"] = it["note"]
        return out
