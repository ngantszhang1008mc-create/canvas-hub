"""Read-only computation over the stored data: SQL over account views, a safe
calculator, and cross-course clash detection.

``run_query`` copies one account's rows into a private in-memory database
(attaching the real one read-only, then detaching it) and only then runs the
query there, under an authorizer that allows nothing but SELECT over those
tables and plain functions. The real database is not reachable from the
query at all, so no name it defines (a CTE, an alias) can stand in for an
allowed table. Other accounts' rows, the assistant's conversations and any
write are out of reach; results are capped in rows and time.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from ..util import InvalidInput
from .deadlines import archived_sql

MAX_ROWS = 500
MAX_SECONDS = 3.0

VIEWS = {
    "items": "SELECT id, item_type, course_canvas_id AS course_id, canvas_id, title, source_url, presence, "
             "first_observed_at, last_observed_at FROM main.items i WHERE account_id={a} AND current_revision>0 "
             f"AND NOT {archived_sql('i', 'main.')}",
    "fields": "SELECT f.item_id, f.field, f.value_json, f.last_observed_at FROM main.item_fields f "
              "JOIN main.items i ON i.id=f.item_id WHERE i.account_id={a} "
              f"AND NOT {archived_sql('i', 'main.')}",
    "courses": "SELECT canvas_id AS course_id, title, source_url FROM main.items WHERE account_id={a} AND item_type='course'",
    "changes": "SELECT id, item_id, kind, field, old_json, new_json, observed_at, baseline FROM main.change_events "
               "WHERE account_id={a}",
    "notes": "SELECT id, kind, title, body, task_state, local_due, item_id, course_canvas_id AS course_id, updated_at "
             "FROM main.annotations WHERE account_id={a}",
    "class_times": "SELECT id, course_canvas_id AS course_id, title, weekday, start_time, end_time, location "
                   "FROM main.class_times WHERE account_id={a}",
    "coverage": "SELECT course_canvas_id AS course_id, category, status, collected_count, reason FROM main.coverage "
                "WHERE account_id={a}",
}
VIEW_HELP = ("Tables (one account only): items(id, item_type, course_id, canvas_id, title, source_url, presence, "
             "first_observed_at, last_observed_at); fields(item_id, field, value_json, last_observed_at) - use "
             "json_extract(value_json, '$') or '$.iso' for dates; courses(course_id, title, source_url); changes(id, item_id, "
             "kind, field, old_json, new_json, observed_at, baseline); notes(id, kind, title, body, task_state, local_due, "
             "item_id, course_id, updated_at); class_times(id, course_id, title, weekday 0=Mon, start_time, end_time, "
             "location); coverage(course_id, category, status, collected_count, reason).")


def run_query(db_path, account_id: int, sql: str) -> dict:
    if not isinstance(sql, str) or not sql.strip():
        raise InvalidInput("sql is required")
    if len(sql) > 8000:
        raise InvalidInput("the query is too long")
    conn = sqlite3.connect(":memory:", uri=True, check_same_thread=False)
    try:
        a = int(account_id)
        # One account's rows, copied before any user SQL runs; the real database is detached again.
        conn.execute("ATTACH DATABASE ? AS src", (f"file:{db_path}?mode=ro",))
        for name, body in VIEWS.items():
            conn.execute(f"CREATE TABLE {name} AS {body.replace('main.', 'src.').format(a=a)}")
        conn.commit()
        conn.execute("DETACH DATABASE src")
        def authorizer(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                # This database holds nothing but the copies (ATTACH is refused below), so any table or CTE
                # read is one of them or built from them; only SQLite's own schema tables stay closed.
                return sqlite3.SQLITE_DENY if (arg1 or "").startswith("sqlite_") else sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_DENY if (arg2 or "").lower() in ("load_extension", "readfile", "writefile") else sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_RECURSIVE:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        deadline = time.monotonic() + MAX_SECONDS
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        try:
            cur = conn.execute(sql)
            rows = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "interrupted" in msg:
                msg = f"the query took longer than {MAX_SECONDS:g} s"
            elif "not authorized" in msg or "prohibited" in msg:
                msg = "only SELECT over these tables is allowed (" + VIEW_HELP + ")"
            raise InvalidInput(f"query failed: {msg}", code="query_failed") from e
        cols = [d[0] for d in cur.description or []]
        out = [list(r) for r in rows[:MAX_ROWS]]          # by position: column names can repeat
        return {"columns": cols, "rows": out, "row_count": len(out), "truncated": len(rows) > MAX_ROWS}
    finally:
        conn.close()


# ---- safe calculator -------------------------------------------------------------------
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Eq: operator.eq,
        ast.NotEq: operator.ne}
_FUN = {"min": min, "max": max, "sum": lambda *a: sum(a[0] if len(a) == 1 and isinstance(a[0], list) else a),
        "round": round, "abs": abs, "ceil": math.ceil, "floor": math.floor, "sqrt": math.sqrt}


def evaluate(expression: str, variables: dict | None = None):
    """Arithmetic, comparisons, and/or/not, a if c else b, min/max/sum/round/abs/ceil/floor/sqrt, lists.
    No attribute access, no calls other than those, no names other than the variables."""
    if not isinstance(expression, str) or len(expression) > 2000:
        raise InvalidInput("expression must be text up to 2,000 characters")
    vars_ = {}
    for k, v in (variables or {}).items():
        if not str(k).isidentifier():
            raise InvalidInput(f"bad variable name {k!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float, list)):
            raise InvalidInput(f"variable {k} must be a number or a list of numbers")
        vars_[k] = v
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise InvalidInput(f"not a valid expression: {e.msg}") from e

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in vars_:
                return vars_[n.id]
            if n.id in ("True", "False"):
                return n.id == "True"
            raise InvalidInput(f"unknown name {n.id!r}")
        if isinstance(n, ast.List):
            return [ev(x) for x in n.elts]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd, ast.Not)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else (+v if isinstance(n.op, ast.UAdd) else not v)
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(right) > 100:
                raise InvalidInput("exponent too large")
            return _BIN[type(n.op)](left, right)
        if isinstance(n, ast.BoolOp):
            vals = [ev(x) for x in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, c in zip(n.ops, n.comparators, strict=True):
                if type(op) not in _CMP:
                    raise InvalidInput("unsupported comparison")
                right = ev(c)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.IfExp):
            return ev(n.body) if ev(n.test) else ev(n.orelse)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUN and not n.keywords:
            return _FUN[n.func.id](*[ev(a) for a in n.args])
        raise InvalidInput(f"not allowed in a calculation: {type(n).__name__}")
    try:
        return ev(tree)
    except ZeroDivisionError as e:
        raise InvalidInput("division by zero") from e


# ---- clashes across courses --------------------------------------------------------------
def find_clashes(hub, account_id: int, days_ahead: int = 21) -> dict:
    """Days with several deadlines, and Canvas events (e.g. exams) overlapping saved class times."""
    acct = hub.identity.get_account(account_id)
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=max(1, min(int(days_ahead), 120)))
    dl = hub.query.deadlines(account_id, start=now.isoformat(), end=end.isoformat(), time_zone=acct.get("time_zone"),
                             include_calendar=True, limit=500)
    by_day: dict[str, list] = {}
    events = []
    for d in dl["deadlines"]:
        day = (d.get("date_local") or "")[:10]
        if d["item_type"] in ("assignment", "quiz"):
            if not ((d.get("submission") or {}).get("submitted_at")):
                by_day.setdefault(day, []).append({"title": d["title"], "course": d["course_title"], "due": d["date_local"],
                                                   "item_id": d["item_id"]})
        else:
            events.append(d)
    busy_days = [{"date": k, "count": len(v), "deadlines": v} for k, v in sorted(by_day.items()) if len(v) >= 2]
    classes = hub.planner.list_classes(account_id)
    clashes = []
    for e in events:
        start = e.get("date_local") or ""
        if len(start) < 16:
            continue
        f = hub.query._fields(e["item_id"])
        endv = f.get("end_at")
        end_local = None
        if isinstance(endv, dict) and endv.get("iso"):
            end_local = datetime.fromisoformat(endv["iso"].replace("Z", "+00:00")).astimezone(
                datetime.fromisoformat(start).tzinfo).isoformat()
        local_date = datetime.fromisoformat(start).date()
        wd = local_date.weekday()
        s_t, e_t = start[11:16], (end_local or start)[11:16]
        for c in classes:
            if (c["valid_from"] and local_date.isoformat() < c["valid_from"]) or (
                    c["valid_until"] and local_date.isoformat() > c["valid_until"]):
                continue
            if c["weekday"] == wd and c["start_time"] < (max(s_t, e_t)) and s_t < c["end_time"]:
                clashes.append({"event": e["title"], "course": e["course_title"], "when": start, "class": c["title"],
                                "class_time": f"{c['start_time']}-{c['end_time']}", "event_item_id": e["item_id"]})
    return {"days_with_several_deadlines": busy_days, "events_overlapping_classes": clashes,
            "class_times_defined": bool(classes), "window_days": int(days_ahead),
            "note": None if classes else "No class times are saved, so class clashes cannot be checked."}


__all__ = ["VIEW_HELP", "evaluate", "find_clashes", "json", "run_query"]
