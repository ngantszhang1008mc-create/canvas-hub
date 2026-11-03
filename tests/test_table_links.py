"""Table links stay with their cells and fail closed on stale/ambiguous IDs."""
import copy
import json

import pytest

from canvas_hub.agent_tools import TOOLS_BY_NAME, ToolContext, run_tool_display
from canvas_hub.assistant import AssistantService
from canvas_hub.boundary import HubAPI
from canvas_hub.table_display import TableLinks


@pytest.fixture
def table_items(hub, account, fixture_pkg):
    package = copy.deepcopy(fixture_pkg)
    course = next(item for item in package["items"] if item["type"] == "course")
    original = next(item for item in package["items"] if item["type"] == "assignment")
    package["items"] = [course]
    package["coverage"] = []
    titles = ["Alpha task", "Beta task", "Gamma task", "Delta task", "Epsilon task", "Zeta task", "Shared title", "Shared title"]
    for canvas_id, title in enumerate(titles, 8101):
        item = copy.deepcopy(original)
        item.update(canvas_id=str(canvas_id), title=title,
                    url=f"https://canvas.example.edu/courses/4242/assignments/{canvas_id}")
        item["fields"]["title"] = title
        package["items"].append(item)
    hub.importer.import_package(package)
    return [dict(row) for row in hub.db.query(
        "SELECT id,title,source_url FROM items WHERE account_id=? AND item_type='assignment' ORDER BY id", (account["id"],))]


def show(hub, account, **args):
    ctx = ToolContext(api=HubAPI(hub), account_id=account["id"])
    text, error, display = run_tool_display(ctx, "show_table", {"columns": ["Course", "Item", "Due"], **args}, {"ui"})
    return json.loads(text), error, display


def test_row_objects_bind_sorted_grouped_rows_and_title_column(hub, account, table_items):
    alpha, beta = table_items[:2]
    rows = [{"cells": ["Group B", beta["title"], "2026-11-09"], "item_id": beta["id"], "detail": "Beta detail"},
            {"cells": ["Group A", alpha["title"], "2026-11-01"], "item_id": alpha["id"], "detail": "Alpha detail"}]
    before = copy.deepcopy(rows)
    result, error, display = show(hub, account, rows=rows, group_by="Course")
    assert not error and "link_warnings" not in result
    assert rows == before
    assert [row["item_id"] for row in display["rows"]] == [beta["id"], alpha["id"]]
    assert [row["link_column"] for row in display["rows"]] == [1, 1]  # never the course column
    assert [row["detail"] for row in display["rows"]] == ["Beta detail", "Alpha detail"]
    assert [row["source_url"] for row in display["rows"]] == [beta["source_url"], alpha["source_url"]]
    assert "item_ids" not in display and "details" not in display
    # The advertised contract accepts objects and legacy cell arrays.
    import jsonschema
    jsonschema.validate({"columns": ["Course", "Item", "Due"], "rows": rows}, TOOLS_BY_NAME["show_table"].schema())
    jsonschema.validate({"columns": ["Item"], "rows": [["plain"]], "item_ids": [None]}, TOOLS_BY_NAME["show_table"].schema())


def test_legacy_parallel_id_reordering_never_guesses_replacement_links(hub, account, table_items):
    alpha, beta = table_items[:2]
    rows = [["Group B", beta["title"], "2026-11-09"], ["Group A", alpha["title"], "2026-11-01"]]
    result, error, display = show(hub, account, rows=rows, item_ids=[alpha["id"], beta["id"]],
                                  details=["Beta detail", "Alpha detail"], group_by="Course")
    assert not error and result["link_warning_count"] == 2
    assert [row["cells"] for row in display["rows"]] == rows
    assert all("item_id" not in row for row in display["rows"])
    assert all("exactly matches" in row["link_warning"] for row in display["rows"])
    assert [row["detail"] for row in display["rows"]] == ["Beta detail", "Alpha detail"]
    _, error, correct = show(hub, account, rows=rows, item_ids=[beta["id"], alpha["id"]])
    assert not error and "link_warnings" not in correct
    assert [row["item_id"] for row in correct["rows"]] == [beta["id"], alpha["id"]]


def test_row_ids_override_conflicting_parallel_ids_with_notice(hub, account, table_items):
    alpha, beta = table_items[:2]
    rows = [{"cells": ["Group", beta["title"]], "item_id": beta["id"]},
            {"cells": ["Group", alpha["title"]], "item_id": alpha["id"]}]
    result, error, display = show(hub, account, rows=rows, item_ids=[alpha["id"], beta["id"]])
    assert not error and result["link_warning_count"] == 2
    assert [row["item_id"] for row in display["rows"]] == [beta["id"], alpha["id"]]
    assert all("takes precedence" in warning for warning in result["link_warnings"])


def test_short_parallel_list_and_invalid_rows_do_not_shift_metadata(hub, account, table_items):
    alpha, beta = table_items[:2]
    _, error, display = show(hub, account, rows=[[alpha["title"]], [beta["title"]]], item_ids=[alpha["id"]])
    assert not error and all("item_id" not in row for row in display["rows"])
    result, error, display = show(hub, account, rows=["malformed", [beta["title"]]], item_ids=[alpha["id"], beta["id"]])
    assert error and display is None and result["error"] == "invalid_table_row"


def test_partial_titles_missing_ids_and_url_mismatches_keep_cells_unlinked(hub, account, table_items):
    alpha, beta = table_items[:2]
    rows = [{"cells": ["Alpha…"], "item_id": alpha["id"]},
            {"cells": [alpha["title"]], "item_id": True},
            {"cells": [alpha["title"]], "item_id": -1},
            {"cells": [alpha["title"]], "item_id": 999999},
            {"cells": [alpha["title"]], "item_id": 2**80},
            {"cells": [alpha["title"]], "item_id": alpha["id"], "source_url": beta["source_url"]}]
    result, error, display = show(hub, account, rows=rows)
    assert not error and result["link_warning_count"] == 6
    assert [row["cells"] for row in display["rows"]] == [row["cells"] for row in rows]
    assert all("item_id" not in row for row in display["rows"])


def test_duplicate_title_requires_exact_source_url_and_never_remaps(hub, account, table_items):
    first, second = table_items[-2:]
    rows = [{"cells": [first["title"]], "item_id": first["id"]},
            {"cells": [first["title"]], "item_id": first["id"], "source_url": first["source_url"]},
            {"cells": [first["title"]], "item_id": first["id"], "source_url": second["source_url"]},
            {"cells": [second["source_url"]], "item_id": second["id"]}]
    result, error, display = show(hub, account, rows=rows)
    assert not error and result["link_warning_count"] == 2
    assert "ambiguous" in display["rows"][0]["link_warning"]
    assert display["rows"][1]["item_id"] == first["id"]
    assert "item_id" not in display["rows"][2]
    assert display["rows"][3]["item_id"] == second["id"]


def test_item_identity_is_scoped_to_the_current_account(hub, account, table_items):
    other = hub.identity.upsert_verified_account(account["instance_id"], {"canvas_user_id": "other-student"})
    alpha = table_items[0]
    _, error, display = show(hub, other, rows=[{"cells": [alpha["title"]], "item_id": alpha["id"], "source_url": alpha["source_url"]}])
    assert not error and "item_id" not in display["rows"][0]
    assert "this account" in display["rows"][0]["link_warning"]
    assert "source_url" not in display["rows"][0]


def test_saved_legacy_tables_are_checked_read_only_without_archive_changes(hub, account, table_items):
    alpha, beta = table_items[:2]
    aid = account["id"]
    svc = AssistantService(hub, HubAPI(hub))
    with hub.db.tx() as conn:
        cid = conn.execute("INSERT INTO assistant_conversations(account_id,created_at,updated_at) VALUES (?,?,?)",
                           (aid, "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z")).lastrowid
    legacy = {"component": "table", "columns": ["Course", "Title"],
              "rows": [["Group", beta["title"]], ["Group", alpha["title"]]],
              "item_ids": [alpha["id"], alpha["id"]], "details": ["Beta detail", "Alpha detail"]}
    svc._append(cid, "tool", {"results": [{"id": "table", "name": "show_table", "content": "{}", "display": legacy}]})
    before = hub.db.one("SELECT content_json FROM assistant_messages WHERE conversation_id=?", (cid,))["content_json"]
    hub.deadlines.set_archived(aid, alpha["id"], True)
    archives = hub.deadlines.archived_ids(aid)
    display = svc.get_conversation(aid, cid)["messages"][0]["results"][0]["display"]
    assert [row["cells"] for row in display["rows"]] == legacy["rows"]
    assert "item_id" not in display["rows"][0]  # wrong legacy target is not repaired
    assert display["rows"][1]["item_id"] == alpha["id"]  # table snapshots keep archived rows
    assert display["rows"][1]["link_column"] == 1
    assert hub.db.one("SELECT content_json FROM assistant_messages WHERE conversation_id=?", (cid,))["content_json"] == before
    assert hub.deadlines.archived_ids(aid) == archives


def test_repeated_saved_tables_share_bounded_identity_queries(hub, account, table_items, monkeypatch):
    real_query, queries = hub.db.query, []
    def record(sql, params=()):
        queries.append(sql)
        return real_query(sql, params)
    monkeypatch.setattr(hub.db, "query", record)
    resolver = TableLinks(hub.db, account["id"])
    data = {"component": "table", "columns": ["Item"], "rows": [[table_items[0]["title"]], ["Missing"]],
            "item_ids": [table_items[0]["id"], 999999]}
    changes = hub.db.conn.total_changes
    for _ in range(20):
        display = resolver.display(data)
        assert display["rows"][0]["item_id"] == table_items[0]["id"]
        assert "item_id" not in display["rows"][1]
    assert len(queries) == 2
    assert hub.db.conn.total_changes == changes


def test_table_renderer_sort_filter_details_and_links_without_browser(hub, account, table_items):
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("node is not installed")
    items = table_items[:6]
    rows = [{"cells": ["Group " + ("B" if i % 2 else "A"), item["title"], i + 1],
             "item_id": item["id"], "detail": item["title"] + " detail"} for i, item in reversed(list(enumerate(items)))]
    _, error, display = show(hub, account, rows=rows, group_by="Course", title="Bound rows", totals=["Due"],
                             column_types=["text", "text", "number"])
    assert not error
    result = subprocess.run(["node", str(Path(__file__).with_name("table_links_renderer.mjs"))],
                            input=json.dumps({"display": display, "items": items}), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
