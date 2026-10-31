"""Deterministic full-text search (SQLite FTS5, bm25 ranking).

Indexed: item titles and searchable fields (bodies, descriptions, comments,
staff replies) plus extracted document text. No LLM or vector service.
"""

from __future__ import annotations

import json
import re

from ..db import Database
from ..htmltools import to_text
from ..model import ITEM_TYPES, spec_for
from ..util import InvalidInput, clamp_limit, decode_cursor, encode_cursor
from .deadlines import archived_sql


def _flatten_json_text(value) -> str:
    parts: list[str] = []

    def walk(v):
        if isinstance(v, str):
            parts.append(to_text(v) if "<" in v and ">" in v else v)
        elif isinstance(v, dict):
            for k in ("author", "comment", "message", "title", "body", "text"):
                if k in v:
                    walk(v[k])
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return "\n".join(p for p in parts if p)


def item_search_text(item_type: str, fields: dict) -> str:
    chunks = []
    for name, value in fields.items():
        spec = spec_for(item_type, name)
        if not spec.searchable or value is None or name == "title":
            continue
        if spec.kind == "html":
            chunks.append(to_text(value))
        elif spec.kind == "text":
            chunks.append(str(value))
        else:
            chunks.append(_flatten_json_text(value))
    return "\n\n".join(c for c in chunks if c)


def _fts_query(q: str) -> str:
    """Turn user input into a safe FTS5 query: quoted terms ANDed together,
    supporting "exact phrases" and trailing * prefix terms."""
    q = (q or "").strip()
    if not q:
        raise InvalidInput("search query is empty")
    if len(q) > 500:
        raise InvalidInput("search query too long")
    tokens = re.findall(r'"([^"]+)"|(\S+)', q)
    terms = []
    for phrase, word in tokens:
        if phrase:
            terms.append('"' + phrase.replace('"', "") + '"')
            continue
        prefix = word.endswith("*")
        w = re.sub(r"[^\w\-./]", " ", word, flags=re.UNICODE).strip()
        for part in w.split():
            terms.append('"' + part + '"' + ("*" if prefix else ""))
    if not terms:
        raise InvalidInput("search query has no searchable terms")
    return " AND ".join(terms)


class SearchService:
    def __init__(self, db: Database):
        self.db = db

    def index_item(self, c, account_id: int, item_id: int, item_type: str, title: str | None, fields: dict) -> None:
        body = item_search_text(item_type, fields)
        self._upsert(c, account_id, item_id, "", title or "", body)

    def index_resource(self, c, account_id: int, item_id: int, resource_id: str, title: str, text: str) -> None:
        self._upsert(c, account_id, item_id, resource_id, title, text)

    def _upsert(self, c, account_id, item_id, resource_id, title, body):
        row = c.execute("SELECT id FROM search_docs WHERE item_id=? AND resource_id=?", (item_id, resource_id)).fetchone()
        if row:
            c.execute("DELETE FROM search_fts WHERE rowid=?", (row["id"],))
            rid = row["id"]
        else:
            rid = c.execute("INSERT INTO search_docs(account_id, item_id, resource_id) VALUES (?,?,?)",
                            (account_id, item_id, resource_id)).lastrowid
        c.execute("INSERT INTO search_fts(rowid, title, body) VALUES (?,?,?)", (rid, title, body))

    def search(self, account_id: int, q: str, *, course_id: str | None = None, types: list[str] | None = None,
               observed_after: str | None = None, observed_before: str | None = None,
               include_documents: bool = True, limit: int | None = None, cursor: str | None = None,
               include_archived: bool = False) -> dict:
        lim = clamp_limit(limit, 20, 100)
        offset = 0
        cur = decode_cursor(cursor)
        if cur:
            if cur.get("q") != q:
                raise InvalidInput("cursor does not belong to this query", code="invalid_cursor")
            offset = int(cur.get("offset", 0))
        if types:
            bad = [t for t in types if t not in ITEM_TYPES]
            if bad:
                raise InvalidInput(f"unknown item types {bad}")
        where = ["search_fts MATCH ?", "d.account_id = ?"]
        params: list = [_fts_query(q), account_id]
        if not include_archived:
            where.append("NOT " + archived_sql())
        if course_id:
            where.append("i.course_canvas_id = ?")
            params.append(str(course_id))
        if types:
            where.append(f"i.item_type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if not include_documents:
            where.append("d.resource_id = ''")
        if observed_after:
            where.append("i.last_observed_at >= ?")
            params.append(observed_after)
        if observed_before:
            where.append("i.last_observed_at < ?")
            params.append(observed_before)
        sql = f"""
            SELECT d.item_id, d.resource_id, i.item_type, i.title, i.course_canvas_id, i.source_url,
                   i.last_observed_at, i.presence, i.current_revision,
                   bm25(search_fts, 4.0, 1.0) AS rank,
                   snippet(search_fts, 1, '[[', ']]', ' … ', 24) AS snippet,
                   snippet(search_fts, 0, '[[', ']]', ' … ', 12) AS title_snippet
            FROM search_fts JOIN search_docs d ON d.id = search_fts.rowid
            JOIN items i ON i.id = d.item_id
            WHERE {' AND '.join(where)}
            ORDER BY rank, d.id
            LIMIT ? OFFSET ?"""
        rows = self.db.query(sql, (*params, lim + 1, offset))
        results = []
        for r in rows[:lim]:
            d = dict(r)
            d["rank"] = round(d["rank"], 4)
            d["limitations"] = self._limitations(account_id, d)
            if d["resource_id"]:
                res = self.db.one("SELECT extraction_status, extraction_detail FROM resources WHERE id=?", (d["resource_id"],))
                d["document"] = {"resource_id": d["resource_id"], "extraction_status": res["extraction_status"] if res else None}
            course = self.db.one("SELECT title FROM items WHERE account_id=? AND item_type='course' AND source_key=?",
                                 (account_id, d["course_canvas_id"])) if d["course_canvas_id"] else None
            d["course_title"] = course["title"] if course else None
            results.append(d)
        next_cursor = encode_cursor({"q": q, "offset": offset + lim}) if len(rows) > lim else None
        return {"query": q, "results": results, "next_cursor": next_cursor}

    def _limitations(self, account_id: int, hit: dict) -> list[str]:
        notes = []
        if hit["presence"] != "observed":
            notes.append(f"item presence: {hit['presence']} (kept as prior evidence)")
        if hit["resource_id"]:
            res = self.db.one("SELECT extraction_status, extraction_detail FROM resources WHERE id=?", (hit["resource_id"],))
            if res and res["extraction_status"] != "ok":
                notes.append(f"document extraction: {res['extraction_status']}")
        stale = self.db.one(
            "SELECT field, last_observed_at FROM item_fields WHERE item_id=? ORDER BY last_observed_at ASC LIMIT 1",
            (hit["item_id"],),
        )
        if stale and stale["last_observed_at"] < hit["last_observed_at"]:
            notes.append(f"some fields older than last observation (oldest: {stale['field']} at {stale['last_observed_at']})")
        return notes

    def rebuild(self, account_id: int | None = None, extracted_text_for=None) -> int:
        """Rebuild the index from stored fields and extracted text."""
        n = 0
        with self.db.tx() as c:
            where = "WHERE account_id=?" if account_id else ""
            ids = [r["id"] for r in c.execute(f"SELECT id FROM search_docs {where}", (account_id,) if account_id else ())]
            for rid in ids:
                c.execute("DELETE FROM search_fts WHERE rowid=?", (rid,))
            c.execute(f"DELETE FROM search_docs {where}", (account_id,) if account_id else ())
            items = c.execute(f"SELECT id, account_id, item_type, title FROM items {where}",
                              (account_id,) if account_id else ()).fetchall()
            for it in items:
                fields = {r["field"]: json.loads(r["value_json"]) for r in
                          c.execute("SELECT field, value_json FROM item_fields WHERE item_id=?", (it["id"],))}
                if fields:
                    self.index_item(c, it["account_id"], it["id"], it["item_type"], it["title"], fields)
                    n += 1
            if extracted_text_for:
                rows = c.execute(
                    f"""SELECT ir.item_id, ir.resource_id, ir.filename, i.account_id, i.title FROM item_resources ir
                        JOIN items i ON i.id=ir.item_id {where.replace('account_id', 'i.account_id')}""",
                    (account_id,) if account_id else (),
                ).fetchall()
                for r in rows:
                    text = extracted_text_for(r["resource_id"])
                    if text:
                        self.index_resource(c, r["account_id"], r["item_id"], r["resource_id"],
                                            r["filename"] or r["title"] or "", text)
        return n
