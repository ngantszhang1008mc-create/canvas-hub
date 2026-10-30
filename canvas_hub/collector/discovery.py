"""Courses -> All Courses inventory.

The All Courses page (/courses) is the authoritative list of enrollments,
including entries that are not on the Dashboard (unpublished, inactive,
past). We read the rendered tables by their header names rather than fixed
CSS classes so that layout differences between Canvas versions degrade into
an explicit 'partial' result instead of silent loss. Invitations are never
accepted and favorites are never changed.
"""

from __future__ import annotations

import hashlib
import re

ALL_COURSES_JS = r"""
() => {
  const text = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const headingFor = (t) => {
    let el = t;
    while (el && el !== document.body) {
      let p = el.previousElementSibling;
      while (p) {
        if (/^H[1-4]$/.test(p.tagName)) return text(p);
        const h = p.querySelector && p.querySelector('h1,h2,h3,h4');
        if (h) return text(h);
        p = p.previousElementSibling;
      }
      el = el.parentElement;
    }
    return '';
  };
  const tables = Array.from(document.querySelectorAll('table'));
  const out = tables.map((t) => {
    let headers = Array.from(t.querySelectorAll('thead th, thead td')).map(text);
    let bodyRows = Array.from(t.querySelectorAll('tbody tr'));
    if (!headers.length && bodyRows.length && bodyRows[0].querySelector('th')) {
      headers = Array.from(bodyRows[0].querySelectorAll('th,td')).map(text);
      bodyRows = bodyRows.slice(1);
    }
    const rows = bodyRows.map((tr) => {
      const ids = new Set();
      tr.querySelectorAll('[data-course-id]').forEach((e) => ids.add(e.getAttribute('data-course-id')));
      if (tr.getAttribute('data-course-id')) ids.add(tr.getAttribute('data-course-id'));
      return {
        id: tr.id || '',
        cls: tr.className || '',
        data_course_ids: Array.from(ids),
        cells: Array.from(tr.querySelectorAll('td,th')).map((td) => ({
          text: text(td),
          cls: td.className || '',
          links: Array.from(td.querySelectorAll('a[href]')).map((a) => ({ href: a.getAttribute('href'), text: text(a) })),
        })),
      };
    });
    return { id: t.id || '', cls: t.className || '', caption: text(t.querySelector('caption')), heading: headingFor(t), headers, rows };
  });
  const content = document.querySelector('#content') || document.body;
  return { tables: out, page_title: document.title, empty_text: text(content).slice(0, 400) };
}
"""

_SECTION_BY_ID = {
    "my_courses_table": "current",
    "past_enrollments_table": "past",
    "future_enrollments_table": "future",
}


def _section(table: dict) -> str | None:
    if table.get("id") in _SECTION_BY_ID:
        return _SECTION_BY_ID[table["id"]]
    label = " ".join([table.get("caption") or "", table.get("heading") or ""]).lower()
    for word, sec in (("past", "past"), ("future", "future"), ("current", "current")):
        if word in label:
            return sec
    return None


def _col(headers: list[str], *names: str) -> int | None:
    low = [h.lower() for h in headers]
    for n in names:
        for i, h in enumerate(low):
            if h == n or h.startswith(n):
                return i
    return None


def parse_all_courses(snapshot: dict) -> dict:
    """Returns {"rows": [...], "tables_seen": n, "problems": [...]}."""
    rows, problems = [], []
    tables = snapshot.get("tables") or []
    course_tables = 0
    for t in tables:
        headers = t.get("headers") or []
        ci = _col(headers, "course")
        if ci is None:
            continue
        course_tables += 1
        sec = _section(t)
        if sec is None:
            problems.append(f"could not tell which enrollment section table '{t.get('heading') or t.get('id')}' is")
            sec = "unknown"
        idx = {
            "nickname": _col(headers, "nickname"),
            "term": _col(headers, "term"),
            "enrolled_as": _col(headers, "enrolled as", "role"),
            "published": _col(headers, "published"),
        }
        for r in t.get("rows") or []:
            cells = r.get("cells") or []
            if ci >= len(cells):
                continue
            cell = cells[ci]
            name = cell.get("text") or ""
            if not name:
                continue
            href = None
            cid = None
            for ln in cell.get("links") or []:
                m = re.search(r"/courses/(\d+)", ln.get("href") or "")
                if m:
                    href, cid = ln["href"], m.group(1)
                    break
            if cid is None and r.get("data_course_ids"):
                cid = r["data_course_ids"][0]

            def val(k, idx=idx, cells=cells):
                i = idx[k]
                return cells[i].get("text") if i is not None and i < len(cells) else None

            entry = {
                "course_id": cid, "name": name, "nickname": val("nickname") or None, "term": val("term"),
                "enrolled_as": val("enrolled_as"), "published_text": val("published"), "section": sec,
                "has_link": href is not None, "href": href,
                "row_classes": r.get("cls") or "",
                "invitation": bool(re.search(r"\binvit", (r.get("cls") or "") + " " + " ".join(c.get("text") or "" for c in cells), re.I)),
            }
            if cid is None:
                basis = "|".join([sec, name, entry["term"] or "", entry["enrolled_as"] or ""])
                entry["unlinked_key"] = "unlinked-" + hashlib.sha256(basis.encode()).hexdigest()[:24]
            rows.append(entry)
    if course_tables == 0:
        problems.append("no enrollment table with a 'Course' column was found on All Courses")
    return {"rows": rows, "tables_seen": course_tables, "problems": problems}


def published_flag(text: str | None) -> bool | None:
    if text is None:
        return None
    t = text.strip().lower()
    if t in ("yes", "published", "true"):
        return True
    if t in ("no", "unpublished", "false"):
        return False
    return None
