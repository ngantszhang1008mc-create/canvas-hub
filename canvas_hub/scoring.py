"""Score models: declarative point/hour buckets and what they add up to.

A model is a list of *buckets* (one per assessed line: an assignment, a
meeting, a block of service hours). Each has a maximum, what has been earned
so far, a status, and an optional gating rule. The user's *state* says which
pending lines they intend to do (include/skip) and any partial values they
expect (points, or hours for hour-based lines). ``evaluate`` turns both into
line results and totals: booked, projected, ceiling, lost, the margin to the
pass threshold and the skip budget.

The same rules run in the browser (``static/components.js``) so the page can
recompute live; the assistant reads totals computed here.

Line rules
- earned:   counts its ``earned`` points (always included); any shortfall is lost.
- lost:     counts ``earned`` (usually 0); the rest is lost.
- locked:   cannot be earned now; counts nothing; the maximum is lost.
- excused:  left out of every total.
- pending:  if included, counts the expected value (the state's value, else
            full marks), subject to gating; if skipped, counts nothing.
Gating
- none:            value counts as is (capped at the maximum).
- all_or_nothing:  counts only at full marks (or full hours), otherwise 0.
- min_threshold:   counts 0 until the value reaches ``min_value``.
Hour lines (``unit: "hours"``) score ``max * min(1, hours / full_at)``;
``min_value`` is then a number of hours (the "zero until minimum hours" rule).
Formula
- a pending line may give ``formula`` (see ``formula.py``) instead of the
  rules above, for scores Canvas only shows as an image, e.g.
  ``if(hours >= 10, 20 * min(1, hours / 20), 0)``. Names: ``value`` (and
  ``hours`` on hour lines), ``max``, ``full_at``, ``min_value``. The result is
  kept between 0 and the maximum; min_threshold gating still applies to the
  value, all_or_nothing to full marks.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import formula as formula_lang
from .util import InvalidInput, parse_iso

STATUSES = ("earned", "pending", "lost", "locked", "excused")
GATING = ("none", "all_or_nothing", "min_threshold")
UNITS = ("points", "hours")
MAX_BUCKETS = 200
MAX_SCENARIOS = 6
FORMULA_NAMES = {"value", "hours", "max", "full_at", "min_value"}
_ID = re.compile(r"^[\w.:-]{1,64}$")


def _num(v: Any, name: str, *, allow_none: bool = False, minimum: float | None = 0.0) -> float | None:
    if v is None or v == "":
        if allow_none:
            return None
        raise InvalidInput(f"{name} is required")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise InvalidInput(f"{name} must be a number")
    if minimum is not None and v < minimum:
        raise InvalidInput(f"{name} must be at least {minimum:g}")
    return float(v)


def normalize_model(spec: dict) -> dict:
    """Validate a model definition; returns a clean copy (raises InvalidInput)."""
    if not isinstance(spec, dict):
        raise InvalidInput("the model must be an object")
    raw = spec.get("buckets")
    if not isinstance(raw, list) or not raw:
        raise InvalidInput("buckets must be a non-empty list")
    if len(raw) > MAX_BUCKETS:
        raise InvalidInput(f"at most {MAX_BUCKETS} buckets")
    buckets, seen = [], set()
    for i, b in enumerate(raw):
        if not isinstance(b, dict):
            raise InvalidInput(f"bucket {i + 1} must be an object")
        bid = str(b.get("id") or f"b{i + 1}")
        if not _ID.match(bid) or bid in seen:
            raise InvalidInput(f"bucket id {bid!r} must be unique (letters, digits, _ . : -)")
        seen.add(bid)
        status = b.get("status") or ("earned" if b.get("earned") is not None else "pending")
        gating = b.get("gating") or "none"
        unit = b.get("unit") or "points"
        if status not in STATUSES:
            raise InvalidInput(f"{bid}: status must be one of {list(STATUSES)}")
        if gating not in GATING:
            raise InvalidInput(f"{bid}: gating must be one of {list(GATING)}")
        if unit not in UNITS:
            raise InvalidInput(f"{bid}: unit must be points or hours")
        mx = _num(b.get("max"), f"{bid}: max")
        earned = _num(b.get("earned"), f"{bid}: earned", allow_none=True)
        min_value = _num(b.get("min_value"), f"{bid}: min_value", allow_none=True)
        if gating == "min_threshold" and min_value is None:
            raise InvalidInput(f"{bid}: min_threshold gating needs min_value")
        full_at = _num(b.get("full_at"), f"{bid}: full_at", allow_none=True)
        if unit == "hours" and not full_at:
            full_at = min_value or mx
        out = {"id": bid, "label": str(b.get("label") or bid)[:200], "max": mx, "earned": earned, "status": status,
               "gating": gating, "min_value": min_value, "unit": unit, "full_at": full_at}
        if b.get("formula"):
            try:
                unknown = formula_lang.names(formula_lang.parse(str(b["formula"]))) - FORMULA_NAMES
            except InvalidInput as e:
                raise InvalidInput(f"{bid}: {e}") from e
            if unknown:
                raise InvalidInput(f"{bid}: formula can use {sorted(FORMULA_NAMES)}, not {sorted(unknown)}")
            out["formula"] = str(b["formula"])
        for k in ("group", "due", "note"):
            if b.get(k) is not None:
                out[k] = str(b[k])[:300]
        if b.get("item_id") is not None:
            try:
                out["item_id"] = int(b["item_id"])
            except (TypeError, ValueError) as e:
                raise InvalidInput(f"{bid}: item_id must be a number") from e
        buckets.append(out)
    threshold = _num(spec.get("threshold"), "threshold", allow_none=True)
    scenarios = []
    for s in (spec.get("scenarios") or [])[:MAX_SCENARIOS]:
        if not isinstance(s, dict) or not s.get("id"):
            raise InvalidInput("each scenario needs an id")
        scenarios.append({"id": str(s["id"])[:40], "label": str(s.get("label") or s["id"])[:60],
                          "include": {k: bool(v) for k, v in (s.get("include") or {}).items() if k in seen},
                          "values": {k: _num(v, f"scenario {s['id']}: {k}") for k, v in (s.get("values") or {}).items()
                                     if k in seen}})
    return {"title": str(spec.get("title") or "Score model")[:200], "threshold": threshold,
            "unit_label": str(spec.get("unit_label") or "pts")[:12], "buckets": buckets, "scenarios": scenarios}


def normalize_state(model: dict, state: dict | None) -> dict:
    state = state if isinstance(state, dict) else {}
    ids = {b["id"] for b in model["buckets"]}
    include = {k: bool(v) for k, v in (state.get("include") or {}).items() if k in ids}
    values = {}
    for k, v in (state.get("values") or {}).items():
        if k in ids and v is not None and v != "":
            values[k] = _num(v, f"value for {k}")
    scenario = state.get("scenario") if any(s["id"] == state.get("scenario") for s in model["scenarios"]) else None
    return {"include": include, "values": values, "scenario": scenario}


def _line(b: dict, state: dict) -> dict:
    mx = b["max"]
    res = {"id": b["id"], "label": b["label"], "max": mx, "status": b["status"], "unit": b["unit"],
           "group": b.get("group"), "points": 0.0, "lost": 0.0, "included": True, "gated": False, "note": None}
    if b["status"] == "excused":
        res.update(included=False, max=0.0)
        return res
    if b["status"] in ("earned", "lost"):     # graded or past: the shortfall is lost for good
        res["points"] = min(mx, b["earned"] or 0.0)
        res["lost"] = mx - res["points"]
        return res
    if b["status"] == "locked":
        res.update(lost=mx, included=False, note="locked")
        return res
    # pending
    included = state["include"].get(b["id"], True)
    res["included"] = included
    if not included:
        res["note"] = "skipped"
        return res
    if b.get("formula"):
        return _formula_line(b, state, res)
    if b["unit"] == "hours":
        hours = state["values"].get(b["id"], b["full_at"] or 0.0)
        res["value"] = hours
        pts = mx * min(1.0, hours / b["full_at"]) if b["full_at"] else mx
        if b["gating"] == "min_threshold" and hours < (b["min_value"] or 0):
            pts, res["gated"], res["note"] = 0.0, True, f"0 until {b['min_value']:g} h"
        elif b["gating"] == "all_or_nothing" and b["full_at"] and hours < b["full_at"]:
            pts, res["gated"], res["note"] = 0.0, True, "all or nothing"
    else:
        val = min(mx, state["values"].get(b["id"], mx))
        res["value"] = val
        pts = val
        if b["gating"] == "all_or_nothing" and val < mx:
            pts, res["gated"], res["note"] = 0.0, True, "all or nothing"
        elif b["gating"] == "min_threshold" and val < (b["min_value"] or 0):
            pts, res["gated"], res["note"] = 0.0, True, f"0 below {b['min_value']:g}"
    res["points"] = round(pts, 4)
    return res


def _formula_line(b: dict, state: dict, res: dict) -> dict:
    mx = b["max"]
    val = state["values"].get(b["id"], b["full_at"] if b["full_at"] is not None else mx)
    res["value"] = val
    env = {"value": val, "hours": val if b["unit"] == "hours" else 0.0, "max": mx, "full_at": b["full_at"] or 0.0,
           "min_value": b["min_value"] or 0.0}
    try:
        pts = min(mx, max(0.0, formula_lang.evaluate(b["formula"], env)))
    except InvalidInput as e:
        pts, res["gated"], res["note"] = 0.0, True, str(e)
    else:
        if b["gating"] == "min_threshold" and val < (b["min_value"] or 0):
            pts, res["gated"], res["note"] = 0.0, True, f"0 until {b['min_value']:g}{' h' if b['unit'] == 'hours' else ''}"
        elif b["gating"] == "all_or_nothing" and pts < mx:
            pts, res["gated"], res["note"] = 0.0, True, "all or nothing"
    res["points"] = round(pts, 4)
    return res


def alerts(model: dict, lines: list[dict], totals: dict, now: datetime | None = None) -> list[dict]:
    """Rule-driven warnings; the page computes the same list."""
    now = now or datetime.now(timezone.utc)
    out = []
    thr = totals.get("threshold")
    by = {b["id"]: b for b in model["buckets"]}
    if thr is not None:
        if totals["ceiling"] < thr - 1e-9:
            out.append({"level": "bad", "text": f"Even full marks on everything left gives {totals['ceiling']:g}, below the pass mark {thr:g}."})
        elif not totals["passes"]:
            out.append({"level": "bad", "text": f"Below the pass mark by {thr - totals['projected']:g} with this plan."})
        elif totals["margin"] < max(1.0, 0.05 * thr):
            out.append({"level": "warn", "text": f"Only {totals['margin']:g} to spare: losing any line worth more fails."})
    for x in lines:
        b = by[x["id"]]
        if x["status"] != "pending" or not x["included"]:
            continue
        due = parse_iso(b["due"]) if b.get("due") and len(b["due"]) > 10 else None
        if x["gated"] and b["unit"] == "hours" and b["gating"] == "min_threshold" and (x.get("value") or 0) < (b["min_value"] or 0):
            out.append({"level": "bad", "text": f"{b['label']}: counts 0 until {b['min_value']:g} h are reached."})
        elif x["gated"]:
            out.append({"level": "bad", "text": f"{b['label']}: {x['note']} — currently worth 0."})
        if due and due < now:
            out.append({"level": "warn", "text": f"{b['label']} is past its due date but still planned."})
        elif b["gating"] == "all_or_nothing" and due and due - now < timedelta(days=7):
            out.append({"level": "warn", "text": f"At risk: {b['label']} is all-or-nothing and due within a week."})
    return out


def evaluate(model: dict, state: dict | None = None) -> dict:
    st = normalize_state(model, state)
    lines = [_line(b, st) for b in model["buckets"]]
    booked = sum(x["points"] for x in lines if x["status"] in ("earned", "lost"))
    projected = sum(x["points"] for x in lines)
    ceiling = booked + sum(x["max"] for x in lines if x["status"] == "pending")
    lost = sum(x["lost"] for x in lines)
    remaining = sum(x["points"] for x in lines if x["status"] == "pending")
    skipped = sum(x["max"] for x in lines if x["status"] == "pending" and not x["included"])
    thr = model.get("threshold")
    totals = {"booked": round(booked, 4), "projected": round(projected, 4), "ceiling": round(ceiling, 4),
              "lost": round(lost, 4), "remaining": round(remaining, 4), "skipped": round(skipped, 4),
              "possible": round(sum(x["max"] for x in lines), 4),
              "open_count": sum(1 for x in lines if x["status"] == "pending" and x["included"]),
              "threshold": thr}
    if thr is not None:
        margin = projected - thr
        totals.update(margin=round(margin, 4), passes=projected >= thr - 1e-9, skip_budget=round(max(0.0, margin), 4),
                      ceiling_passes=ceiling >= thr - 1e-9)
    groups: dict[str, dict] = {}
    for x in lines:
        g = groups.setdefault(x["group"] or "Other", {"earned": 0.0, "remaining": 0.0, "lost": 0.0, "skipped": 0.0})
        if x["status"] in ("earned", "lost"):
            g["earned"] += x["points"]
        elif x["status"] == "pending":
            g["remaining" if x["included"] else "skipped"] += x["points"] if x["included"] else x["max"]
        g["lost"] += x["lost"]
    return {"lines": lines, "totals": totals, "groups": groups, "state": st, "alerts": alerts(model, lines, totals)}
