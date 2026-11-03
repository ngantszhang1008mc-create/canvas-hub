"""Local, deterministic document chunks with versioned original-text ranges.

No embeddings, model summaries or remote requests. The index follows search_docs
ownership/deletion, including replacement of current attachment revisions.
"""
from __future__ import annotations

import hashlib
import json
import re

from ..util import Conflict, InvalidInput, NotFound

CHUNK_CHARS = 1400


def index_source(c, doc_id, item_id, resource_id, title, body):
    version = hashlib.sha256((title + "\0" + body).encode()).hexdigest()
    old = c.execute("SELECT version FROM context_sources WHERE search_doc_id=?", (doc_id,)).fetchone()
    if old and old["version"] == version:
        return
    c.execute("DELETE FROM context_sources WHERE search_doc_id=?", (doc_id,))
    source_id = f"document:{item_id}:{resource_id}" if resource_id else f"item:{item_id}"
    chunks, start, section, page = [], 0, None, 1 if "\f" in body else None
    while start < len(body):
        end = min(len(body), start + CHUNK_CHARS)
        if end < len(body):
            boundary = body.rfind("\n", max(start + 1, end - 300), end)
            if boundary > start:
                end = boundary + 1
        text = body[start:end]
        heading = re.match(r"#{1,6}\s+([^\n]+)", text)
        if heading:
            section = heading[1][:160]
        chunks.append((start, end, section, page, text))
        headings = re.findall(r"(?m)^#{1,6}\s+([^\n]+)", text)
        if headings:
            section = headings[-1][:160]
        if page is not None:
            page += text.count("\f")
        start = end
    c.execute("INSERT INTO context_sources VALUES (?,?,?,?,?,?,?)",
              (doc_id, source_id, version, title[:300], body[:280], len(body), len(chunks)))
    for ordinal, (start, end, section, page, text) in enumerate(chunks):
        row = c.execute("INSERT INTO context_chunks(search_doc_id,ordinal,start_char,end_char,section,page,text) VALUES (?,?,?,?,?,?,?)",
                        (doc_id, ordinal, start, end, section, page, text)).lastrowid
        c.execute("INSERT INTO context_chunk_fts(rowid,text) VALUES (?,?)", (row, title + "\n" + text))


class ContextSources:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _scope(account_id, course_id=None, item_id=None):
        where, args = ["d.account_id=?"], [account_id]
        if course_id is not None:
            where.append("i.course_canvas_id=?")
            args.append(str(course_id))
        if item_id is not None:
            where.append("i.id=?")
            args.append(item_id)
        return " AND ".join(where), args

    def _backfill(self, account_id, course_id=None, item_id=None):
        # Existing installs migrate lazily, at most 10 stored sources per read.
        # The response reports remaining unindexed sources; repeat catalog reads
        # to finish. Newly imported/re-extracted sources update synchronously.
        where, args = self._scope(account_id, course_id, item_id)
        with self.db.tx() as c:
            rows = c.execute("SELECT d.id,d.item_id,d.resource_id,f.title,f.body FROM search_docs d JOIN items i ON i.id=d.item_id "
                             "JOIN search_fts f ON f.rowid=d.id LEFT JOIN context_sources s ON s.search_doc_id=d.id "
                             f"WHERE {where} AND s.search_doc_id IS NULL ORDER BY d.id LIMIT 10", args).fetchall()
            for row in rows:
                index_source(c, row["id"], row["item_id"], row["resource_id"], row["title"], row["body"])

    def coverage(self, account_id, course_id=None, item_id=None):
        where, args = self._scope(account_id, course_id, item_id)
        row = self.db.one("SELECT COUNT(*) AS total, COUNT(s.search_doc_id) AS indexed FROM search_docs d "
                          "JOIN items i ON i.id=d.item_id LEFT JOIN context_sources s ON s.search_doc_id=d.id "
                          f"WHERE {where}", args)
        filters, values = ["i.account_id=?", "i.item_type='file'"], [account_id]
        if course_id is not None:
            filters.append("i.course_canvas_id=?")
            values.append(str(course_id))
        if item_id is not None:
            filters.append("i.id=?")
            values.append(item_id)
        missing = self.db.one("SELECT COUNT(*) AS n FROM items i WHERE " + " AND ".join(filters) +
                              " AND NOT EXISTS (SELECT 1 FROM search_docs d JOIN context_sources s ON s.search_doc_id=d.id "
                              "WHERE d.item_id=i.id AND d.resource_id!='' AND s.total_chars>0)", values)["n"]
        return {"indexed_sources": row["indexed"], "unindexed_sources": row["total"] - row["indexed"],
                "files_without_indexed_text": missing,
                "note": "Counts cover locally collected material only. Use get_coverage for collection gaps. Search hits are not exhaustive reading coverage."}

    @staticmethod
    def _metadata(row):
        return {k: row[k] for k in ("source_id", "version", "title", "total_chars", "chunk_count", "item_id",
                                    "resource_id", "course_canvas_id", "source_url", "current_revision", "presence")}

    def catalog(self, account_id, *, course_id=None, item_id=None, offset=0, limit=15, include_preview=False,
                byte_limit=None, response_byte_limit=None):
        self._backfill(account_id, course_id, item_id)
        where, args = self._scope(account_id, course_id, item_id)
        limit, offset = max(1, min(20, int(limit))), max(0, int(offset))
        rows = self.db.query("SELECT s.*,d.item_id,d.resource_id,i.course_canvas_id,i.source_url,i.current_revision,i.presence "
                             "FROM context_sources s JOIN search_docs d ON d.id=s.search_doc_id JOIN items i ON i.id=d.item_id "
                             f"WHERE {where} ORDER BY s.source_id LIMIT ? OFFSET ?", (*args, limit + 1, offset))
        metadata = {
            "preview_included": bool(include_preview),
            "preview_kind": "opening excerpt, not a complete summary" if include_preview else None,
            "note": "Metadata only by default. Set include_preview=true for opening excerpts, or read_context_source with source_id/version for full original ranges. Listing does not establish reading coverage.",
            "coverage": self.coverage(account_id, course_id, item_id), "untrusted_data": True,
        }

        def page(sources):
            return {"sources": sources,
                    "next_offset": offset + len(sources) if len(rows) > len(sources) else None, **metadata}

        sources, size = [], 0
        for row in rows[:limit]:
            source = self._metadata(row)
            if include_preview:
                source['preview'] = row['preview']
            cost = len(json.dumps(source, ensure_ascii=False).encode('utf-8'))
            if sources and byte_limit is not None and size + cost > byte_limit:
                break
            # In-app catalogs fit their final envelope directly, rather than
            # requiring snapshot pages merely to discover source IDs. A single
            # oversized row still reaches the snapshot fallback intact.
            if sources and response_byte_limit is not None and len(json.dumps(
                    page([*sources, source]), ensure_ascii=False, default=str).encode()) > response_byte_limit:
                break
            sources.append(source)
            size += cost
        return page(sources)

    def search(self, account_id, query, *, course_id=None, item_id=None, source_id=None, offset=0, limit=2):
        from .search import _fts_query
        self._backfill(account_id, course_id, item_id)
        where, args = self._scope(account_id, course_id, item_id)
        if source_id is not None:
            where += " AND s.source_id=?"
            args.append(source_id)
        limit, offset = max(1, min(4, int(limit))), max(0, int(offset))
        rows = self.db.query("SELECT s.*,d.item_id,d.resource_id,i.course_canvas_id,i.source_url,i.current_revision,i.presence, "
                             "ch.ordinal,ch.start_char,ch.end_char,ch.section,ch.page,ch.text "
                             "FROM context_chunk_fts JOIN context_chunks ch ON ch.id=context_chunk_fts.rowid "
                             "JOIN context_sources s ON s.search_doc_id=ch.search_doc_id JOIN search_docs d ON d.id=s.search_doc_id "
                             "JOIN items i ON i.id=d.item_id WHERE context_chunk_fts MATCH ? AND " + where +
                             " ORDER BY bm25(context_chunk_fts),ch.id LIMIT ? OFFSET ?", (_fts_query(query), *args, limit + 1, offset))
        return {"query": query, "chunks": [{**self._metadata(r), **{k: r[k] for k in ("ordinal", "start_char", "end_char", "section", "page", "text")},
                                             "range_kind": "extracted text characters; page only when extractor retained form feeds"} for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
                "coverage": self.coverage(account_id, course_id, item_id), "untrusted_data": True,
                "complete_read": False}

    def read(self, account_id, source_id, *, version=None, offset=0, limit=4000):
        # Backfill belongs to the catalog; source IDs come only from that catalog
        # or the chunk search. Unknown/cross-account IDs reveal nothing.
        with self.db.tx():
            row = self.db.one("SELECT s.*,d.item_id,d.resource_id,i.course_canvas_id,i.source_url,i.current_revision,i.presence "
                              "FROM context_sources s JOIN search_docs d ON d.id=s.search_doc_id JOIN items i ON i.id=d.item_id "
                              "WHERE d.account_id=? AND s.source_id=?", (account_id, source_id))
            if not row:
                raise NotFound("source not found for this account; list current sources", code="source_not_found")
            if version is not None and version != row["version"]:
                raise Conflict("Source changed; restart traversal using its current version.", code="source_version_conflict")
            offset, limit = max(0, int(offset)), max(1, min(8000, int(limit)))
            if offset > row["total_chars"]:
                raise InvalidInput("offset is past the end of this source")
            end = min(row["total_chars"], offset + limit)
            chunks = self.db.query("SELECT * FROM context_chunks WHERE search_doc_id=? AND end_char>? AND start_char<? ORDER BY ordinal",
                                   (row["search_doc_id"], offset, end))
            text = "".join(r["text"][max(0, offset-r["start_char"]):min(len(r["text"]), end-r["start_char"])] for r in chunks)
            return {**self._metadata(row), "text": text, "start_char": offset, "end_char": end,
                    "next_offset": end if end < row["total_chars"] else None,
                    "range_kind": "original extracted text, character offsets", "untrusted_data": True,
                    "coverage": {"returned_chars": len(text), "total_chars": row["total_chars"],
                                 "complete_read": offset == 0 and end == row["total_chars"]}}
