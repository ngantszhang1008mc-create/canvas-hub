"""Item types, field kinds and comparison rules.

Every Canvas thing the app tracks is an *item* of one of ITEM_TYPES. Fields
are typed so that change detection can ignore harmless differences
(formatting, volatile counters, signed-URL tokens) without hiding meaningful
edits.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import dates
from .htmltools import normalized_for_compare
from .util import canonical_json, sha256_text

ITEM_TYPES = {
    "course": "Course",
    "syllabus": "Syllabus",
    "page": "Page",
    "announcement": "Announcement",
    "assignment": "Assignment",
    "submission": "Submission / grade",
    "quiz": "Quiz",
    "discussion": "Discussion",
    "module": "Module",
    "module_item": "Module item",
    "folder": "Folder",
    "file": "File",
    "grade_summary": "Grade summary",
    "calendar_event": "Calendar event",
    "group": "Group",
    "conversation": "Inbox conversation",
    "external_tool": "Linked tool",
    "external_link": "External link",
}

# Collection categories (scope + coverage granularity).
CATEGORIES = {
    "courses": "Course inventory (All Courses)",
    "home": "Home and syllabus",
    "announcements": "Announcements",
    "assignments": "Assignments and submissions",
    "quizzes": "Quizzes (listing only)",
    "grades": "Grades and feedback",
    "modules": "Modules",
    "pages": "Pages",
    "files": "Files",
    "discussions": "Discussions",
    "calendar": "Calendar",
    "groups": "Groups",
    "inbox": "Inbox",
    "external_tools": "Linked tools and recordings",
    "linked_content": "Content reached through Home/module/body links",
}
DEFAULT_CATEGORIES = [
    "courses", "home", "announcements", "assignments", "quizzes", "grades", "modules", "pages",
    "files", "discussions", "calendar", "external_tools", "linked_content",
]
OPT_IN_CATEGORIES = ["groups", "inbox"]

COVERAGE_STATUSES = ("complete", "partial", "blocked", "not_exposed", "not_checked")
DEPTHS = ("complete", "partial", "not_checked", "not_applicable", "blocked")


@dataclass(frozen=True)
class FieldSpec:
    kind: str = "json"           # text | html | date | number | bool | json
    volatile: bool = False       # stored, but never produces a change event
    searchable: bool = False


_HTML = FieldSpec("html", searchable=True)
_DATE = FieldSpec("date")
_VOL = FieldSpec("json", volatile=True)

FIELD_SPECS: dict[str, dict[str, FieldSpec]] = {
    "*": {
        "title": FieldSpec("text", searchable=True),
        "body": _HTML,
        "description": _HTML,
        "source_updated_at": _DATE,
        "created_at": _DATE,
        "posted_at": _DATE,
        "due_at": _DATE,
        "unlock_at": _DATE,
        "lock_at": _DATE,
        "start_at": _DATE,
        "end_at": _DATE,
        "read_state": _VOL,
        "unread_count": _VOL,
        "last_reply_at": FieldSpec("date", volatile=True),
        "reply_count": _VOL,
        "subscribed": _VOL,
        "is_favorite": _VOL,
        "message": _HTML,
        "author": FieldSpec("text"),
    },
    "course": {"name": FieldSpec("text", searchable=True), "course_code": FieldSpec("text", searchable=True)},
    "submission": {"submitted_at": _DATE, "graded_at": _DATE, "posted_at": _DATE,
                   "comments": FieldSpec("json", searchable=True)},
    "discussion": {"staff_replies": FieldSpec("json", searchable=True)},
    "file": {"modified_at": _DATE, "display_name": FieldSpec("text", searchable=True)},
    "folder": {"modified_at": _DATE},
    "conversation": {"messages": FieldSpec("json", searchable=True), "last_message_at": _DATE,
                     "workflow_state": _VOL},
    "calendar_event": {"location_name": FieldSpec("text", searchable=True)},
}


def spec_for(item_type: str, field: str) -> FieldSpec:
    t = FIELD_SPECS.get(item_type, {})
    if field in t:
        return t[field]
    return FIELD_SPECS["*"].get(field, FieldSpec())


def compare_hash(item_type: str, field: str, value) -> str:
    spec = spec_for(item_type, field)
    if value is None:
        basis = "null"
    elif spec.kind == "html":
        basis = "html:" + normalized_for_compare(value if isinstance(value, str) else str(value))
    elif spec.kind == "date":
        basis = "date:" + str(dates.compare_key(value))
    elif spec.kind == "text":
        basis = "text:" + " ".join(str(value).split())
    else:
        basis = "json:" + canonical_json(value)
    return sha256_text(basis)
