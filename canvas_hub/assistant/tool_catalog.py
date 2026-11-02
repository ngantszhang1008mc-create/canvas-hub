"""Request-local tool advertisement. Discovery never executes or grants a tool.

The registry remains the execution/permission authority. Historical tool groups
are not rewritten when this request's advertised tools change.
"""
from __future__ import annotations

import json
import re

from ..agent_tools import AgentTool, tools_for, validate_args
from ..util import InvalidInput
from .context import estimate

GROUPS = (
    ("tracker", "Trackers & scores", "Read current tracker inputs, local totals and guarded state patches."),
    ("documents", "Documents & sources", "Search and read bounded stored document excerpts with source coverage."),
    ("courses", "Courses & changes", "Read courses, items, changes and collection coverage."),
    ("deadlines", "Deadlines", "Read deadlines and manage local views, archives and completion."),
    ("notes", "Notes & files", "Read and edit local notes, reminders and reusable files."),
    ("planner", "Planner", "Read timetables, class times and scheduling clashes."),
    ("collect", "Collection", "Check sign-in and collection jobs; refresh stored Canvas data."),
    ("external", "External sources", "Read external-source records and agent view requests."),
    ("display", "Interactive answers", "Show tables, charts, artifacts and other supported displays."),
    ("writes", "Canvas write previews", "Prepare Canvas writes only; sending still requires approval in the app."),
    ("settings", "Scope settings", "Read structured data and manage collection scope when permitted."),
    ("other", "Other tools", "Additional permitted tools from the shared registry."),
)
GROUP_NAMES = {g[0] for g in GROUPS}
DEFAULTS = {"list_courses", "search", "get_item", "get_deadlines", "get_component_context", "create_note", "start_collection"}
MAX_ADVERTISED = 32
MAX_GATEWAY_TOOLS = 128  # Fixed registry protocol; full context is budgeted by Policy.
COMMAND = re.compile(r"^/([a-z][a-z0-9_-]*)(?=\s|$)")


def group_for(name):
    if any(s in name for s in ("component", "score", "grading", "grade", "checklist", "hours_tracker", "overall_widget")):
        return "tracker"
    if name in {"list_context_sources", "search_context_sources", "read_context_source", "read_document"}:
        return "documents"
    if name in {"list_accounts", "list_courses", "search", "get_item", "get_item_history", "list_items", "get_announcements",
                "get_changes", "get_coverage", "get_freshness", "whats_new"}:
        return "courses"
    if "deadline" in name or name in {"find_dates_in_pages", "set_assignment_done_locally"}:
        return "deadlines"
    if any(s in name for s in ("note", "reminder", "file")):
        return "notes"
    if name in {"get_timetable", "list_class_times", "add_class_time", "remove_class_time", "find_clashes", "show_timetable"}:
        return "planner"
    if name in {"get_signin_status", "list_jobs", "get_job", "start_collection", "wait_for_job", "cancel_job"}:
        return "collect"
    if "external" in name or "view_request" in name or name in {"submit_source_analysis", "submit_view_result"}:
        return "external"
    if name.startswith("prepare_") or name == "list_canvas_writes":
        return "writes"
    if name.startswith("show_"):
        return "display"
    if name in {"query_data", "calculate", "update_scope"}:
        return "settings"
    return "other"


def definition(tool):
    return {"name": tool.name, "description": tool.description, "schema": tool.schema()}


DISCOVER = AgentTool("discover_tools",
    "Find additional permitted Canvas Hub tools. No selection: page/search names and groups. "
    "select replaces the request's discovered selection (manual selections stay). It only loads schemas, never executes tools. "
    "Use group:tracker or tool:get_component_context. On Codex, invoke newly discovered tools with call_discovered_tool.", "read",
    {"query": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
     "select": {"type": "array", "items": {"type": "string"}},
     "refresh_schemas": {"type": "boolean", "description": "Explicitly reread selected Codex schemas if their earlier definitions are needed again."}})
DISPATCH = AgentTool("call_discovered_tool",
    "Invoke one already loaded tool by name and its schema-validated arguments. Primarily for Codex's fixed native tool list. "
    "Discover/load a permitted tool first. This cannot grant permissions or confirm Canvas writes.", "read",
    {"name": {"type": "string"}, "arguments": {"type": "object"}}, ["name", "arguments"])


class ToolCatalog:
    def __init__(self, permissions, *, allowed_names=None, discovery=True):
        order = {g[0]: i for i, g in enumerate(GROUPS)}
        self.discovery = discovery
        self.tools = {t.name: t for t in sorted(tools_for(permissions), key=lambda t: (order[group_for(t.name)], t.name))
                      if allowed_names is None or t.name in allowed_names}
        self.groups = [{"id": g, "label": label, "description": desc,
                        "tools": [n for n in self.tools if group_for(n) == g]} for g, label, desc in GROUPS]
        self.groups = [g for g in self.groups if g["tools"]]

    def normalize(self, selection):
        if selection is None:
            return []
        if not isinstance(selection, list) or len(selection) > 32 or any(not isinstance(s, str) for s in selection):
            raise InvalidInput("tool_selection must be an array of at most 32 tool/group identifiers")
        values = set()
        for token in selection:
            if ":" not in token:
                token = ("group:" if token in GROUP_NAMES else "tool:") + token
            kind, name = token.split(":", 1)
            if kind == "group" and any(g["id"] == name for g in self.groups) or kind == "tool" and name in self.tools:
                values.add(token)
            else:
                raise InvalidInput(f"Unknown or no longer permitted tool selection: {token}. Refresh the tool picker.", code="invalid_tool_selection")
        order = ["group:" + g["id"] for g in self.groups] + ["tool:" + n for n in self.tools]
        return [v for v in order if v in values]

    def names(self, selection):
        if not self.discovery and not selection:
            # A reviewed gateway uses its own fixed registry, not our discovery
            # protocol. Keep its approved tools reachable when no chips are set.
            return set(self.tools)
        names = DEFAULTS & self.tools.keys()
        for token in self.normalize(selection):
            kind, name = token.split(":", 1)
            names |= {name} if kind == "tool" else {n for n in self.tools if group_for(n) == name}
        return names

    def advertised(self, selection):
        names = self.names(selection)
        result = [definition(t) for n, t in self.tools.items() if n in names]
        if self.discovery:
            result += [definition(DISCOVER), definition(DISPATCH)]
        # Schema tokens share the actual request budget with system, history and
        # sources. Policy measures all of them before every provider dispatch;
        # a separate fixed token slice can reject an otherwise fitting request.
        # Keep the independent tool-count bound and never truncate definitions.
        if self.discovery and len(result) > MAX_ADVERTISED:
            raise InvalidInput("Selected tools exceed the 32-tool request budget. Choose fewer groups or individual tools from the picker.", code="tool_selection_too_large")
        if not self.discovery and len(result) > MAX_GATEWAY_TOOLS:
            raise InvalidInput("The reviewed tool catalog exceeds the gateway protocol limit.", code="tool_selection_too_large")
        return result

    def public(self):
        return {"groups": self.groups, "tools": [{"name": n, "group": group_for(n), "description": t.description,
                 "schema_tokens": estimate(definition(t)) + 32} for n, t in self.tools.items()],
                "default_tools": [n for n in self.tools if n in DEFAULTS], "discovery_available": self.discovery,
                "limits": {"max_advertised": MAX_ADVERTISED if self.discovery else MAX_GATEWAY_TOOLS,
                           "schema_tokens": None},
                "selection_semantics": "For the next message only; adds tools to the defaults. Does not execute tools or change permissions."}

    def parse_message(self, text, selection):
        selected = self.normalize(selection)
        prompt = text.strip()
        while match := COMMAND.match(prompt):
            selected = self.normalize([*selected, match[1]])
            prompt = prompt[match.end():].lstrip()
        if not prompt:
            raise InvalidInput("Tool selection does not send a request. Add a question before sending.", code="tool_selection_only")
        self.advertised(selected)  # fail before storing a message or creating a provider
        return prompt, selected


class ToolSession:
    """A run's fixed manual choices plus replaceable discovered choices."""
    def __init__(self, permissions, manual=None, *, allowed_names=None, discovery=True):
        self.allowed_names, self.discovery_enabled = allowed_names, discovery
        self.catalog = ToolCatalog(permissions, allowed_names=allowed_names, discovery=discovery)
        self.manual = self.catalog.normalize(manual)
        self.discovered = []
        # Codex receives the initial schemas natively. Later discovery supplies
        # only schemas that have not yet been given to this same native turn.
        self._provided_schemas = set(self.catalog.names(self.manual))

    def refresh(self, permissions):
        self.catalog = ToolCatalog(permissions, allowed_names=self.allowed_names, discovery=self.discovery_enabled)
        # Revocations remove schemas immediately, never become new grants.
        def permitted(tokens):
            kept = []
            for token in tokens:
                try:
                    kept.extend(self.catalog.normalize([token]))
                except InvalidInput:
                    pass
            return kept
        self.manual, self.discovered = permitted(self.manual), permitted(self.discovered)

    def advertised(self):
        return self.catalog.advertised(self.manual + self.discovered)

    def discover(self, args, *, include_schemas=False):
        if not self.discovery_enabled:
            raise InvalidInput("This gateway uses a reviewed fixed tool list. Select tools from that list before sending; discovery cannot extend its policy.")
        args = validate_args(DISCOVER, args)
        if "select" in args:
            selected = self.catalog.normalize(args["select"])
            self.catalog.advertised(self.manual + selected)  # atomic: rejected selection changes nothing
            self.discovered = selected
        query = (args.get("query") or "").casefold()[:200]
        offset = args.get("offset") or 0
        out = {"manual_selection": self.manual, "discovered_selection": self.discovered,
               "loaded_tools": [t["name"] for t in self.advertised()], "executed": False,
               "note": "Selection only loads tools. To change discovered tools, pass select again. Manual choices remain for this message."}
        if 'select' not in args or 'query' in args or 'offset' in args:
            public = self.catalog.public()
            matches = [t for t in public["tools"] if query in (t["name"] + " " + t["group"] + " " + t["description"]).casefold()]
            out.update(groups=[{k: g[k] for k in ("id", "label", "description")} for g in public["groups"]],
                       tools=[{**t, "description": t["description"][:220]} for t in matches[offset:offset+15]],
                       next_offset=offset + 15 if offset + 15 < len(matches) else None)
        if include_schemas and "select" in args:
            chosen = self.catalog.names(self.manual + self.discovered)
            needed = chosen if args.get('refresh_schemas') else chosen - self._provided_schemas
            out["schemas"] = [definition(t) for n, t in self.catalog.tools.items() if n in needed]
            out["schemas_already_available"] = sorted(chosen & self._provided_schemas)
            self._provided_schemas.update(chosen)
            out["invocation"] = "Use call_discovered_tool with the tool name and arguments matching its schema."
        return json.dumps(out, ensure_ascii=False, separators=(',', ':'))

    def dispatch(self, args):
        if not self.discovery_enabled:
            raise InvalidInput("This gateway does not enable the discovery dispatcher.")
        args = validate_args(DISPATCH, args)
        if args["name"] not in self.catalog.names(self.manual + self.discovered):
            raise InvalidInput("Tool is not currently loaded or permitted; use discover_tools first.", code="tool_not_loaded")
        return args["name"], args["arguments"]
