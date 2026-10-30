"""What-if grade projection and hour tracking (pure functions).

Grade projector
- categories with a weight (percent of the course grade) and items with
  max points and an actual score (None = not graded yet);
- the user's state adds *hypothetical* scores for ungraded items;
- like Canvas, a weighted grade only counts categories that have counted
  items, re-normalising their weights; with no weights, points are pooled;
- maps the percentage to a letter with the course's scale, and tells what
  uniform score on the remaining items reaches each letter.

Hours tracker
- logged entries add up to a total; nothing counts until ``minimum`` hours
  (then points scale up to ``target``); if the deadline passes below the
  minimum the line is lost (the "reset if below minimum" rule);
- ``refreshed_at`` records when the total was last checked against the
  source (e.g. the service-hours portal).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .scoring import _num
from .util import InvalidInput, parse_iso

_ID = re.compile(r"^[\w.:-]{1,64}$")
MAX_CATEGORIES = 60
MAX_ITEMS = 500             # per category; larger definitions are refused, never cut short
DEFAULT_SCALE = [{"letter": "A", "min": 90}, {"letter": "B", "min": 80}, {"letter": "C", "min": 70},
                 {"letter": "D", "min": 60}, {"letter": "F", "min": 0}]


# ---- grade projector -------------------------------------------------------------------
def normalize_projector(spec: dict) -> dict:
    if not isinstance(spec, dict) or not isinstance(spec.get("categories"), list) or not spec["categories"]:
        raise InvalidInput("categories must be a non-empty list")
    if len(spec["categories"]) > MAX_CATEGORIES:
        raise InvalidInput(f"at most {MAX_CATEGORIES} categories")
    cats, seen = [], set()
    for ci, c in enumerate(spec["categories"]):
        if not isinstance(c, dict):
            raise InvalidInput(f"category {ci + 1} must be an object")
        cid = str(c.get("id") or f"c{ci + 1}")
        items = []
        if len(c.get("items") or []) > MAX_ITEMS:
            raise InvalidInput(f"category {c.get('name') or cid}: at most {MAX_ITEMS} items")
        for ii, it in enumerate(c.get("items") or []):
            iid = str(it.get("id") or f"{cid}.{ii + 1}")
            if not _ID.match(iid) or iid in seen:
                raise InvalidInput(f"item id {iid!r} must be unique")
            seen.add(iid)
            out = {"id": iid, "label": str(it.get("label") or iid)[:200], "max": _num(it.get("max"), f"{iid}: max"),
                   "score": _num(it.get("score"), f"{iid}: score", allow_none=True)}
            if it.get("item_id") is not None:
                out["item_id"] = int(it["item_id"])
            items.append(out)
        cats.append({"id": cid, "name": str(c.get("name") or cid)[:120],
                     "weight": _num(c.get("weight"), f"{cid}: weight", allow_none=True), "items": items,
                     "drop_lowest": int(c.get("drop_lowest") or 0)})
    scale = spec.get("scale") or DEFAULT_SCALE
    if not isinstance(scale, list) or not all(isinstance(x, dict) and "letter" in x and "min" in x for x in scale):
        raise InvalidInput("scale must be a list of {letter, min}")
    scale = sorted(({"letter": str(x["letter"])[:4], "min": _num(x["min"], "scale min")} for x in scale),
                   key=lambda x: -x["min"])
    return {"title": str(spec.get("title") or "Grade projection")[:200], "categories": cats, "scale": scale,
            "weighted": any((c["weight"] or 0) > 0 for c in cats), "target": spec.get("target")}


def _letter(scale, pct):
    for s in scale:
        if pct >= s["min"] - 1e-9:
            return s["letter"]
    return scale[-1]["letter"] if scale else None


def project(spec: dict, state: dict | None = None) -> dict:
    hyp = {k: float(v) for k, v in ((state or {}).get("hypothetical") or {}).items()
           if isinstance(v, (int, float)) and not isinstance(v, bool)}

    def cat_pct(c, fill: float | None = None):
        """Percent for a category; fill = uniform percent for still-ungraded items (None = leave out)."""
        pts = []
        for it in c["items"]:
            s = it["score"] if it["score"] is not None else hyp.get(it["id"])
            if s is None and fill is not None:
                s = it["max"] * fill / 100
            if s is not None and it["max"] > 0:
                pts.append((min(s, it["max"] * 2), it["max"]))
        if c["drop_lowest"] and len(pts) > c["drop_lowest"]:
            pts = sorted(pts, key=lambda p: p[0] / p[1])[c["drop_lowest"]:]
        got, mx = sum(p[0] for p in pts), sum(p[1] for p in pts)
        return (100 * got / mx if mx else None), got, mx

    def overall(fill: float | None = None):
        rows = [(c, *cat_pct(c, fill)) for c in spec["categories"]]
        if spec["weighted"]:
            counted = [(c, p) for c, p, _, _ in rows if p is not None and (c["weight"] or 0) > 0]
            w = sum(c["weight"] for c, _ in counted)
            return (sum(c["weight"] * p for c, p in counted) / w if w else None), rows
        got, mx = sum(r[2] for r in rows), sum(r[3] for r in rows)
        return (100 * got / mx if mx else None), rows

    actual_pct, _ = overall(None) if not hyp else (None, None)
    pct, rows = overall(None)
    cats = [{"id": c["id"], "name": c["name"], "weight": c["weight"], "percent": None if p is None else round(p, 2),
             "points": round(g, 2), "possible": round(m, 2)} for c, p, g, m in rows]
    remaining = [it for c in spec["categories"] for it in c["items"] if it["score"] is None and it["id"] not in hyp]
    needed = []
    if remaining:
        for s in spec["scale"]:
            lo, hi = 0.0, 200.0
            best, _ = overall(hi)
            if best is None or best < s["min"]:
                needed.append({"letter": s["letter"], "needs": None, "reachable": False})
                continue
            for _ in range(40):
                mid = (lo + hi) / 2
                v, _ = overall(mid)
                if v is not None and v >= s["min"]:
                    hi = mid
                else:
                    lo = mid
            needed.append({"letter": s["letter"], "needs": round(hi, 1), "reachable": hi <= 100 + 1e-6})
    return {"percent": None if pct is None else round(pct, 2), "letter": _letter(spec["scale"], pct) if pct is not None else None,
            "categories": cats, "remaining_items": len(remaining), "needed_on_remaining": needed,
            "weights_missing": not spec["weighted"] and len(spec["categories"]) > 1,
            "actual_percent": None if actual_pct is None else round(actual_pct, 2)}


# ---- hours tracker -----------------------------------------------------------------------
def normalize_hours(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise InvalidInput("the tracker must be an object")
    target = _num(spec.get("target"), "target")
    minimum = _num(spec.get("minimum"), "minimum", allow_none=True) or 0.0
    if minimum > target:
        raise InvalidInput("minimum cannot exceed target")
    deadline = spec.get("deadline")
    if deadline and not parse_iso(str(deadline)):
        raise InvalidInput("deadline must be an ISO-8601 time with an offset")
    return {"title": str(spec.get("title") or "Hours")[:200], "target": target, "minimum": minimum,
            "points": _num(spec.get("points"), "points", allow_none=True), "deadline": deadline,
            "unit": str(spec.get("unit") or "h")[:8], "source": str(spec.get("source") or "")[:300] or None}


def clean_hours_state(state: dict) -> dict:
    entries = []
    for i, e in enumerate((state or {}).get("entries") or []):
        if not isinstance(e, dict):
            continue
        h = _num(e.get("hours"), "hours")
        d = str(e.get("date") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            raise InvalidInput("each entry needs a date YYYY-MM-DD")
        entries.append({"id": str(e.get("id") or f"e{i + 1}")[:40], "date": d, "hours": h, "note": str(e.get("note") or "")[:300]})
    r = (state or {}).get("refreshed_at")
    if r and not parse_iso(str(r)):
        raise InvalidInput("refreshed_at must be an ISO-8601 time")
    return {"entries": sorted(entries, key=lambda e: e["date"]), "refreshed_at": r}


def hours_summary(spec: dict, state: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    dl = parse_iso(spec["deadline"]) if spec.get("deadline") else None
    # Only hours done by the deadline (its own calendar date) count; later entries cannot revive a lost line.
    last_day = spec["deadline"][:10] if dl else None
    entries = state.get("entries") or []
    total = round(sum(e["hours"] for e in entries if not last_day or e["date"] <= last_day), 2)
    late = round(sum(e["hours"] for e in entries if last_day and e["date"] > last_day), 2)
    days_left = (dl - now).total_seconds() / 86400 if dl else None
    if total >= spec["minimum"]:
        frac = min(1.0, total / spec["target"]) if spec["target"] else 1.0
        status = "complete" if total >= spec["target"] else "counting"
    else:
        frac = 0.0
        status = "lost" if dl and dl < now else "below_minimum"
    pts = round(spec["points"] * frac, 2) if spec.get("points") is not None else None
    left = max(0.0, spec["target"] - total)
    per_week = round(left / max(days_left / 7, 1 / 7), 2) if days_left and days_left > 0 and left else None
    return {"total": total, "minimum": spec["minimum"], "target": spec["target"], "status": status, "points": pts,
            "hours_to_minimum": max(0.0, round(spec["minimum"] - total, 2)), "hours_to_target": round(left, 2),
            "days_left": None if days_left is None else round(days_left, 1), "needed_per_week": per_week,
            "after_deadline": late,
            "refreshed_at": state.get("refreshed_at")}
