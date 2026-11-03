"""Bind table navigation to row content, without changing stored chat snapshots.

A supplied item ID is never repaired by guessing from a title. New row objects
keep cells and provenance together; legacy arrays are paired before any view
sorting and get the same account-scoped, fail-closed checks.
"""
from __future__ import annotations

from .util import InvalidInput


class TableLinks:
    """Read-only resolver, shared across one conversation response to cache lookups."""

    def __init__(self, db=None, account_id=None):
        self.db = db
        self.account_id = account_id
        self.items = {}
        self.title_counts = {}

    def _load(self, ids):
        missing = ids - self.items.keys()
        if not missing or self.db is None or self.account_id is None:
            return
        # At most 200 IDs/titles per table. Cache absent IDs too; repeated saved
        # tables in a conversation must not repeat the same database reads.
        self.items.update(dict.fromkeys(missing))
        marks = ",".join("?" for _ in missing)
        loaded = self.db.query(
            f"SELECT id,title,source_url FROM items WHERE account_id=? AND id IN ({marks})",
            (self.account_id, *sorted(missing)),
        )
        for item in loaded:
            self.items[item["id"]] = dict(item)
        titles = {item["title"] for item in loaded if item["title"]} - self.title_counts.keys()
        if titles:
            marks = ",".join("?" for _ in titles)
            for row in self.db.query(
                f"SELECT title,COUNT(*) AS n FROM items WHERE account_id=? AND title IN ({marks}) GROUP BY title",
                (self.account_id, *sorted(titles)),
            ):
                self.title_counts[row["title"]] = row["n"]

    def display(self, data, *, strict=False):
        if not data or data.get("component") != "table":
            return data
        columns = data.get("columns") or []
        raw_rows = data.get("rows") or []
        legacy_ids = data.get("item_ids") or []
        legacy_details = data.get("details") or []
        warnings = [str(warning)[:400] for warning in (data.get("link_warnings") or [])[:200]]
        rows = []
        # A short/stale parallel list has no safe interpretation. Preserve the
        # cells and details, but do not attach any of those IDs.
        legacy_aligned = not legacy_ids or len(legacy_ids) == len(raw_rows)
        for i, raw in enumerate(raw_rows[:200]):
            bound = isinstance(raw, dict)
            cells = raw.get("cells") if bound else raw
            malformed = not isinstance(cells, list)
            if malformed:
                if strict:
                    raise InvalidInput(f"table row {i + 1} needs a cells array", code="invalid_table_row")
                # Historical malformed content remains readable, never linked.
                cells = [str(raw)]
            row = {"cells": [v if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)[:300]
                             for v in cells[:len(columns)]]}
            detail = raw.get("detail") if bound else legacy_details[i] if i < len(legacy_details) else None
            if detail:
                row["detail"] = str(detail)[:4000]
            iid = raw.get("item_id") if bound else legacy_ids[i] if i < len(legacy_ids) else None
            if bound and raw.get("source_url") is not None:
                row["source_url"] = raw["source_url"]
            if bound and raw.get("link_warning"):
                row["link_warning"] = str(raw["link_warning"])[:300]
            if bound and i < len(legacy_ids) and legacy_ids[i] != iid:
                warnings.append(f"Row {i + 1}: ignored parallel item_ids; the row's own item_id takes precedence.")
            if malformed and iid is not None:
                row["link_warning"] = "Item link omitted: the saved row has no cells array."
            elif not bound and not legacy_aligned and iid is not None:
                row["link_warning"] = "Item link omitted: the parallel item_ids length does not match rows."
            elif iid is not None:
                if type(iid) is int and 0 < iid <= 2**53 - 1:
                    row["item_id"] = iid
                else:
                    row["link_warning"] = "Item link omitted: item_id must be a positive safe integer."
            rows.append(row)
        self._load({r["item_id"] for r in rows if "item_id" in r})
        for i, row in enumerate(rows):
            if "item_id" in row:
                reason, column = self._check(row)
                if reason:
                    row.pop("item_id")
                    row.pop("source_url", None)
                    row["link_warning"] = "Item link omitted: " + reason
                else:
                    row["link_column"] = column
                    # Canonical source provenance travels with the validated
                    # row rather than in a second independently ordered array.
                    row["source_url"] = self.items[row["item_id"]]["source_url"]
                    row.pop("link_warning", None)
            if row.get("link_warning"):
                warnings.append(f"Row {i + 1}: {row['link_warning']}")
        result = {k: v for k, v in data.items() if k not in ("rows", "item_ids", "details", "link_warnings")}
        result["rows"] = rows
        if warnings:
            result["link_warnings"] = list(dict.fromkeys(warnings))
        return result

    def _check(self, row):
        item = self.items.get(row["item_id"])
        if not item:
            return "the item is not available in this account.", None
        cells = row["cells"]
        title_columns = [i for i, value in enumerate(cells) if item["title"] and value == item["title"]]
        url_columns = [i for i, value in enumerate(cells) if item["source_url"] and value == item["source_url"]]
        source_url = row.get("source_url")
        if source_url is not None and source_url != item["source_url"]:
            return "source_url does not match item_id.", None
        if not title_columns and not url_columns:
            return "no visible cell exactly matches the item's stored title or source URL.", None
        if not url_columns and self.title_counts.get(item["title"], 0) > 1 and not source_url:
            return "the title is ambiguous; include the item's exact source_url in its row object.", None
        return None, (title_columns or url_columns)[0]
