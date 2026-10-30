"""A synthetic Canvas server for automated tests and offline demos.

It mimics the *shape* of Canvas behaviour observed on a real instance (see
docs/CLAUDE_BRIEF.md section 11) with entirely made-up, anonymized data:

* interactive login with a second (MFA) step and a session cookie
* Courses -> All Courses page with current/past tables and an unpublished,
  unlinked enrollment
* courses whose navigation hides Pages/Files while Home links to them
* asynchronously rendered All Courses table, paginated announcements
* nested folders, module view-to-complete requirements, locks/prerequisites
* JSON responses prefixed with ``while(1);`` as Canvas does for session auth

It records every request so tests can assert that no write (non-GET) request
was made and that view-to-complete content was not opened.

Run standalone:  python -m canvas_hub.testing.fake_canvas --port 8765
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import secrets
import threading
import zlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

USER = {"id": "5001", "name": "Alex Student", "short_name": "Alex"}
PROFILE = {"id": "5001", "name": "Alex Student", "login_id": "astudent", "primary_email": "astudent@example.edu",
           "time_zone": "Asia/Hong_Kong", "locale": None}
PASSWORD = "correct horse"
MFA_CODE = "246810"


def make_pdf(text_lines: list[str]) -> bytes:
    content = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({t}) Tj T*" for t in text_lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_png() -> bytes:
    import struct

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"\x00\xff\x00\x00" * 1
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _f(fid, name, ctype, data, folder, *, updated="2026-09-01T02:00:00Z", locked=False):
    return {"id": fid, "display_name": name, "filename": name.replace(" ", "_"), "content-type": ctype,
            "size": len(data), "created_at": "2026-09-01T02:00:00Z", "updated_at": updated, "modified_at": updated,
            "folder_id": folder, "locked": False, "hidden": False, "locked_for_user": locked, "_data": data}


def build_dataset() -> dict:
    """Synthetic data. IDs are arbitrary; nothing is real course content."""
    ann = []
    for i in range(1, 36):
        ann.append({
            "id": str(7000 + i), "title": f"Week {i} update", "posted_at": f"2026-09-{(i % 28) + 1:02d}T01:00:00Z",
            "message": f"<p>Notes for week {i}. Office hours unchanged.</p>",
            "author": {"id": "9001", "display_name": "Dr. Instructor"}, "html_url": f"/courses/101/discussion_topics/{7000+i}",
            "read_state": "unread", "unread_count": 0, "discussion_subentry_count": 0, "attachments": [],
            "locked": False, "require_initial_post": False, "discussion_type": "side_comment",
        })
    ann[0]["message"] = ('<p>Reminder: <a href="/courses/101/assignments/1001">Problem Set 1</a> is due on Oct 30 at 11:59pm. '
                         'See also <a href="https://external.example.org/reading">the reading</a>.</p>')
    ann[1]["attachments"] = [{"id": "8801", "display_name": "week2-slides.pdf", "filename": "week2-slides.pdf",
                              "content-type": "application/pdf", "size": 0, "url": "/files/8801/download?verifier=abc",
                              "updated_at": "2026-09-03T01:00:00Z", "modified_at": "2026-09-03T01:00:00Z"}]

    files_101 = {
        "100": {"id": "100", "name": "course files", "full_name": "course files", "parent_folder_id": None},
        "110": {"id": "110", "name": "Lectures", "full_name": "course files/Lectures", "parent_folder_id": "100"},
        "111": {"id": "111", "name": "Old", "full_name": "course files/Lectures/Old", "parent_folder_id": "110"},
        "120": {"id": "120", "name": "Tutorials", "full_name": "course files/Tutorials", "parent_folder_id": "100"},
        "130": {"id": "130", "name": "Exams", "full_name": "course files/Exams", "parent_folder_id": "100"},
        "140": {"id": "140", "name": "Media", "full_name": "course files/Media", "parent_folder_id": "100"},
    }
    file_list = []
    for i in range(1, 22):
        file_list.append(_f(str(8000 + i), f"handout-{i:02d}.txt", "text/plain",
                            f"Handout {i}: linear maps and eigenvalues, section {i}.".encode(), "100"))
    for i in range(1, 15):
        file_list.append(_f(str(8100 + i), f"lecture-{i:02d}.pdf", "application/pdf",
                            make_pdf([f"Lecture {i}", "Eigenvalues and eigenvectors", "Spectral theorem"]), "110"))
    file_list.append(_f("8201", "old-notes.md", "text/markdown", b"# Old notes\nDiagonalization recap.", "111"))
    file_list.append(_f("8301", "tutorial-1.html", "text/html", b"<html><body><h1>Tutorial 1</h1><p>Row reduction.</p></body></html>", "120"))
    file_list.append(_f("8401", "scan-midterm.pdf", "application/pdf", make_pdf([]) + b"\n" * 300000, "130"))
    file_list.append(_f("8501", "diagram.png", "image/png", make_png(), "140"))
    file_list.append(_f("8502", "lecture-video.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000, "140"))
    file_list.append(_f("8801", "week2-slides.pdf", "application/pdf", make_pdf(["Week 2 slides", "Gaussian elimination"]), "100"))
    file_list.append(_f("9001", "lecture-notes-1.pdf", "application/pdf", make_pdf(["Honors lecture notes 1", "Graph colouring"]), "200"))
    file_list.append(_f("9002", "problem-sheet.txt", "text/plain", b"Problem sheet: prove the four colour lemma.", "200"))
    file_list.append(_f("9301", "seminar-reading.pdf", "application/pdf", make_pdf(["Seminar reading", "Ethics of AI"]), "300"))
    file_list.append(_f("9302", "must-view-handout.txt", "text/plain", b"This handout completes a module requirement.", "300"))

    return {
        "courses": {
            "101": {"id": "101", "name": "DEMO1010 Linear Algebra (Fall 2026)", "course_code": "DEMO1010",
                    "workflow_state": "available", "default_view": "syllabus", "time_zone": "Asia/Hong_Kong",
                    "term": {"name": "2026-27 Fall"}, "start_at": None, "end_at": None,
                    "syllabus_body": ('<h2>Syllabus</h2><p>Grading: homework 30%, midterm 30%, final 40%.</p>'
                                      '<p>Office hours: Tue 14:00-16:00, Room 1234.</p>'
                                      '<p><a href="/courses/101/files/8001">Handout 1</a></p>'),
                    "teachers": [{"id": "9001", "display_name": "Dr. Instructor"}],
                    "apply_assignment_group_weights": True, "hide_final_grades": False,
                    "tabs": ["home", "announcements", "assignments", "grades", "discussions", "pages", "files", "syllabus"],
                    "section": "current", "published": True, "role": "Student"},
            "102": {"id": "102", "name": "DEMO2020H Honors Discrete Math", "course_code": "DEMO2020H",
                    "workflow_state": "available", "default_view": "wiki", "time_zone": "Asia/Hong_Kong",
                    "term": {"name": "2026-27 Fall"}, "syllabus_body": None,
                    "teachers": [{"id": "9002", "display_name": "Prof. Honors"}],
                    "tabs": ["home", "announcements", "assignments", "grades"],
                    "section": "current", "published": True, "role": "Student"},
            "103": {"id": "103", "name": "DEMO1000A Seminar on AI", "course_code": "DEMO1000A",
                    "workflow_state": "available", "default_view": "wiki", "time_zone": "Asia/Hong_Kong",
                    "term": {"name": "2026-27 Fall"}, "syllabus_body": None,
                    "teachers": [{"id": "9003", "display_name": "Dr. Seminar"}],
                    "tabs": ["home", "modules", "announcements", "grades", "context_external_tool_55"],
                    "section": "current", "published": True, "role": "Student"},
            "104": {"id": "104", "name": "DEMO3999 Unpublished Lab", "section": "current", "published": False,
                    "role": "Student", "unlinked": True, "term": {"name": "2026-27 Fall"}},
            "105": {"id": "105", "name": "DEMO0900 Bridging Course (Summer 2026)", "course_code": "DEMO0900",
                    "workflow_state": "completed", "default_view": "modules", "time_zone": "Asia/Hong_Kong",
                    "term": {"name": "2025-26 Summer"}, "syllabus_body": None, "teachers": [],
                    "tabs": ["home", "modules"], "section": "past", "published": True, "role": "Student"},
        },
        "pages": {
            "101": [
                {"page_id": "6101", "url": "course-policies", "title": "Course policies", "published": True, "front_page": False,
                 "updated_at": "2026-09-02T03:00:00Z", "body": "<p>Late work loses 10% per day.</p>"},
                {"page_id": "6102", "url": "exam-info", "title": "Exam information", "published": True, "front_page": False,
                 "updated_at": "2026-09-05T03:00:00Z", "body": "<p>Midterm on Oct 20 in Hall A.</p>"},
            ],
            "102": [
                {"page_id": "6201", "url": "home", "title": "Welcome to DEMO2020H", "published": True, "front_page": True,
                 "updated_at": "2026-09-01T03:00:00Z",
                 "body": ('<p>Welcome!</p><ul><li><a href="/courses/102/pages/lecture-notes">Lecture notes</a></li>'
                          '<li><a href="/courses/102/files/9002/download?wrap=1">Problem sheet</a></li></ul>')},
                {"page_id": "6202", "url": "lecture-notes", "title": "Lecture notes", "published": True, "front_page": False,
                 "updated_at": "2026-09-08T03:00:00Z",
                 "body": '<p>Week 1: <a href="/courses/102/files/9001?verifier=zzz&wrap=1">lecture-notes-1.pdf</a></p>'},
            ],
            "103": [
                {"page_id": "6301", "url": "start-here", "title": "Start here", "published": True, "front_page": True,
                 "updated_at": "2026-09-01T03:00:00Z",
                 "body": ('<p>Welcome to the seminar.</p><details><summary>Course logistics (click to expand)</summary>'
                          '<p>We meet Thursdays 10:00 in Room 2001. Contact: seminar-staff@example.edu</p></details>'
                          '<p>See <a href="/courses/103/modules">Modules</a>.</p>')},
                {"page_id": "6302", "url": "week-1-reading", "title": "Week 1 reading", "published": True, "front_page": False,
                 "updated_at": "2026-09-02T03:00:00Z", "body": "<p>Read chapter 1 and reflect.</p>"},
                {"page_id": "6303", "url": "integrity-pledge", "title": "Integrity pledge", "published": True, "front_page": False,
                 "updated_at": "2026-09-02T03:00:00Z", "body": "<p>Viewing this page completes the requirement.</p>"},
            ],
        },
        "announcements": {"101": ann, "102": [], "103": [
            {"id": "7301", "title": "Seminar starts", "posted_at": "2026-09-01T01:00:00Z", "message": "<p>See you Thursday.</p>",
             "author": {"id": "9003", "display_name": "Dr. Seminar"}, "html_url": "/courses/103/discussion_topics/7301",
             "read_state": "read", "unread_count": 0, "discussion_subentry_count": 0, "attachments": []}]},
        "discussions": {"101": [
            {"id": "7501", "title": "Questions about PS1", "posted_at": "2026-09-10T01:00:00Z",
             "message": "<p>Ask questions here.</p>", "author": {"id": "9001", "display_name": "Dr. Instructor"},
             "html_url": "/courses/101/discussion_topics/7501", "read_state": "read", "unread_count": 2,
             "discussion_subentry_count": 3, "attachments": [], "require_initial_post": False,
             "_view": {"participants": [{"id": "9001", "display_name": "Dr. Instructor"},
                                        {"id": "5100", "display_name": "Classmate One"}],
                       "view": [{"id": 1, "user_id": "5100", "message": "<p>Is Q3 about kernels?</p>",
                                 "created_at": "2026-09-11T01:00:00Z",
                                 "replies": [{"id": 2, "user_id": "9001", "message": "<p>Yes, kernels of linear maps.</p>",
                                              "created_at": "2026-09-11T03:00:00Z"}]},
                                {"id": 3, "user_id": "5100", "message": "<p>Thanks!</p>", "created_at": "2026-09-11T04:00:00Z"}]}},
            {"id": "7502", "title": "Introduce yourself", "posted_at": "2026-09-01T01:00:00Z",
             "message": "<p>Post first to see others.</p>", "author": {"id": "9001", "display_name": "Dr. Instructor"},
             "html_url": "/courses/101/discussion_topics/7502", "read_state": "read", "unread_count": 0,
             "discussion_subentry_count": 20, "attachments": [], "require_initial_post": True, "_view_forbidden": True},
        ]},
        "assignment_groups": {"101": [{"id": "1", "name": "Homework", "group_weight": 30},
                                      {"id": "2", "name": "Exams", "group_weight": 70}],
                              "102": [{"id": "3", "name": "Assignments", "group_weight": 100}],
                              "103": [{"id": "4", "name": "Participation", "group_weight": 100}]},
        "assignments": {
            "101": [
                {"id": "1001", "name": "Problem Set 1", "description": "<p>Solve problems 1-10. Show working.</p>",
                 "due_at": "2026-10-31T15:59:00Z", "unlock_at": "2026-10-01T00:00:00Z", "lock_at": "2026-11-07T15:59:00Z",
                 "points_possible": 10, "grading_type": "points", "submission_types": ["online_upload"],
                 "assignment_group_id": "1", "published": True, "html_url": "/courses/101/assignments/1001",
                 "rubric": [{"description": "Correctness", "points": 8, "ratings": [{"description": "Full", "points": 8}]},
                            {"description": "Presentation", "points": 2, "ratings": []}],
                 "submission": {"assignment_id": "1001", "workflow_state": "submitted", "submitted_at": "2026-10-31T16:20:00Z",
                                "late": True, "missing": False, "excused": False, "score": None, "grade": None,
                                "attempt": 1, "seconds_late": 1260, "posted_at": None, "submission_type": "online_upload"}},
                {"id": "1002", "name": "Problem Set 2", "description": "<p>Problems on determinants.</p>",
                 "due_at": "2026-11-14T15:59:00Z", "points_possible": 10, "grading_type": "points",
                 "submission_types": ["online_upload"], "assignment_group_id": "1", "published": True,
                 "html_url": "/courses/101/assignments/1002",
                 "submission": {"assignment_id": "1002", "workflow_state": "unsubmitted", "submitted_at": None,
                                "late": False, "missing": False, "excused": False, "score": None, "grade": None}},
                {"id": "1003", "name": "Midterm", "description": "<p>In-person midterm.</p>", "due_at": "2026-10-20T02:00:00Z",
                 "points_possible": 100, "grading_type": "points", "submission_types": ["on_paper"],
                 "assignment_group_id": "2", "published": True, "html_url": "/courses/101/assignments/1003",
                 "submission": {"assignment_id": "1003", "workflow_state": "graded", "submitted_at": None,
                                "late": False, "missing": False, "excused": False, "score": None, "grade": None,
                                "posted_at": None}},
                {"id": "1004", "name": "Bonus essay", "description": None, "due_at": None, "points_possible": 0,
                 "grading_type": "pass_fail", "submission_types": ["online_text_entry"], "assignment_group_id": "1",
                 "published": True, "locked_for_user": True, "lock_explanation": "This assignment is locked until Dec 1.",
                 "html_url": "/courses/101/assignments/1004",
                 "submission": {"assignment_id": "1004", "workflow_state": "unsubmitted", "excused": True,
                                "late": False, "missing": False, "score": None, "grade": None}},
            ],
            "102": [
                {"id": "2001", "name": "Homework A", "description": "<p>Colour the graph.</p>", "due_at": "2026-11-05T15:59:00Z",
                 "points_possible": 20, "grading_type": "points", "submission_types": ["online_upload"],
                 "assignment_group_id": "3", "published": True, "html_url": "/courses/102/assignments/2001",
                 "submission": {"assignment_id": "2001", "workflow_state": "graded", "submitted_at": "2026-11-01T10:00:00Z",
                                "late": False, "missing": False, "excused": False, "score": 18, "grade": "18",
                                "posted_at": "2026-11-03T00:00:00Z", "attempt": 1}},
                {"id": "2002", "name": "Homework B", "description": "<p>Prove it.</p>", "due_at": "2026-10-01T15:59:00Z",
                 "points_possible": 20, "grading_type": "points", "submission_types": ["online_upload"],
                 "assignment_group_id": "3", "published": True, "html_url": "/courses/102/assignments/2002",
                 "submission": {"assignment_id": "2002", "workflow_state": "unsubmitted", "missing": True,
                                "late": False, "excused": False, "score": 0, "grade": "0", "posted_at": "2026-10-05T00:00:00Z"}},
            ],
            "103": [
                {"id": "3001", "name": "Reflection 1", "description": "<p>500 words.</p>", "due_at": "2026-11-10T15:59:00Z",
                 "points_possible": 5, "grading_type": "points", "submission_types": ["online_text_entry"],
                 "assignment_group_id": "4", "published": True, "html_url": "/courses/103/assignments/3001",
                 "submission": {"assignment_id": "3001", "workflow_state": "unsubmitted", "late": False, "missing": False,
                                "excused": False, "score": None, "grade": None}},
            ],
        },
        "comments": {"2001": [{"author_name": "Prof. Honors", "comment": "Good work on part (b).",
                               "created_at": "2026-11-03T00:00:00Z", "attachments": []}]},
        "quizzes": {"103": [{"id": "4301", "title": "Week 1 check", "description": "<p>Ten questions.</p>",
                             "quiz_type": "assignment", "due_at": "2026-11-12T15:59:00Z", "time_limit": 20,
                             "allowed_attempts": 1, "points_possible": 10, "question_count": 10, "published": True,
                             "html_url": "/courses/103/quizzes/4301", "assignment_id": None}]},
        "modules": {
            "103": [
                {"id": "5301", "name": "Week 1", "position": 1, "unlock_at": None, "prerequisite_module_ids": [],
                 "require_sequential_progress": False, "state": "started", "published": True,
                 "items": [
                     {"id": "53011", "title": "Week 1 reading", "type": "Page", "page_url": "week-1-reading", "position": 1,
                      "indent": 0, "html_url": "/courses/103/modules/items/53011",
                      "completion_requirement": {"type": "must_view", "completed": True}},
                     {"id": "53012", "title": "Integrity pledge", "type": "Page", "page_url": "integrity-pledge", "position": 2,
                      "indent": 0, "html_url": "/courses/103/modules/items/53012",
                      "completion_requirement": {"type": "must_view", "completed": False}},
                     {"id": "53013", "title": "Seminar reading (PDF)", "type": "File", "content_id": "9301", "position": 3,
                      "indent": 1, "html_url": "/courses/103/modules/items/53013"},
                     {"id": "53014", "title": "Must-view handout", "type": "File", "content_id": "9302", "position": 4,
                      "indent": 1, "html_url": "/courses/103/modules/items/53014",
                      "completion_requirement": {"type": "must_view", "completed": False}},
                     {"id": "53015", "title": "Reflection 1", "type": "Assignment", "content_id": "3001", "position": 5,
                      "indent": 0, "html_url": "/courses/103/modules/items/53015",
                      "completion_requirement": {"type": "must_submit", "completed": False},
                      "content_details": {"due_at": "2026-11-10T15:59:00Z", "points_possible": 5}},
                     {"id": "53016", "title": "Week 1 check", "type": "Quiz", "content_id": "4301", "position": 6, "indent": 0,
                      "html_url": "/courses/103/modules/items/53016"},
                     {"id": "53017", "title": "Recorded lecture (external)", "type": "ExternalTool", "position": 7, "indent": 0,
                      "external_url": "https://video.example.org/launch?id=1", "html_url": "/courses/103/modules/items/53017"},
                     {"id": "53018", "title": "Further reading", "type": "ExternalUrl", "position": 8, "indent": 0,
                      "external_url": "https://journal.example.org/article/42", "html_url": "/courses/103/modules/items/53018"},
                     {"id": "53019", "title": "Readings", "type": "SubHeader", "position": 9, "indent": 0},
                 ]},
                {"id": "5302", "name": "Week 2 (locked)", "position": 2, "unlock_at": "2026-12-01T00:00:00Z",
                 "prerequisite_module_ids": ["5301"], "require_sequential_progress": True, "state": "locked",
                 "published": True, "items": None,
                 "_items": [{"id": "53021", "title": "Week 2 reading", "type": "Page", "page_url": "week-2-reading",
                             "position": 1, "indent": 0, "html_url": "/courses/103/modules/items/53021",
                             "content_details": {"locked_for_user": True,
                                                 "lock_explanation": "This module is locked until Dec 1."}}]},
            ],
            "105": [{"id": "5501", "name": "Summer module", "position": 1, "state": "completed", "items": []}],
        },
        "folders": {"101": list(files_101.values()),
                    "102": [{"id": "200", "name": "course files", "full_name": "course files", "parent_folder_id": None}],
                    "103": [{"id": "300", "name": "course files", "full_name": "course files", "parent_folder_id": None}]},
        "files": {f["id"]: f for f in file_list},
        "enrollments": {"101": {"current_score": 82.5, "final_score": 41.2, "current_grade": None, "final_grade": None},
                        "102": {"current_score": 45.0, "final_score": 45.0, "current_grade": "D", "final_grade": "D"},
                        "103": {"current_score": None, "final_score": None}},
        "calendar": [
            {"id": "c1", "title": "Midterm review session", "start_at": "2026-10-18T06:00:00Z", "end_at": "2026-10-18T08:00:00Z",
             "location_name": "Hall A", "context_code": "course_101", "type": "event", "html_url": "/calendar?event_id=c1"},
            {"id": "c2", "title": "Seminar guest talk", "start_at": "2026-11-06T02:00:00Z", "end_at": "2026-11-06T03:00:00Z",
             "location_name": "Room 2001", "context_code": "course_103", "type": "event", "html_url": "/calendar?event_id=c2"},
        ],
        "groups": [{"id": "6601", "name": "PS group 3", "course_id": "101", "members_count": 4, "context_type": "Course"}],
        "conversations": [{"id": "6701", "subject": "Regrade request", "context_code": "course_101", "message_count": 2,
                           "last_message_at": "2026-10-02T01:00:00Z", "workflow_state": "unread",
                           "participants": [{"id": "5001", "name": "Alex Student"}, {"id": "9001", "name": "Dr. Instructor"}],
                           "messages": [{"id": 1, "author_id": "9001", "created_at": "2026-10-02T01:00:00Z", "body": "Regrade done."},
                                        {"id": 2, "author_id": "5001", "created_at": "2026-10-01T01:00:00Z", "body": "Could you check Q2?"}]}],
    }


class FakeCanvas:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, page_size: int = 10, async_courses_ms: int = 600):
        self.data = build_dataset()
        self.sessions: set[str] = set()
        self.pending_mfa: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self.page_size = page_size
        self.async_courses_ms = async_courses_ms
        self.fail_paths: dict[str, int] = {}   # path prefix -> HTTP status to force
        self.expire_after_api_requests: int | None = None   # simulate a session expiring mid-run
        self.api_requests = 0
        self.user = dict(USER)
        self.profile = dict(PROFILE)
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                server._handle(self, "GET")

            def do_POST(self):
                server._handle(self, "POST")

            def do_PUT(self):
                server._handle(self, "PUT")

            def do_DELETE(self):
                server._handle(self, "DELETE")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def expire_sessions(self):
        with self.lock:
            self.sessions.clear()

    def paths_requested(self) -> list[str]:
        return [p for _, p in self.requests]

    # ---- plumbing ----------------------------------------------------------------
    def _session(self, h) -> str | None:
        c = SimpleCookie(h.headers.get("Cookie") or "")
        tok = c.get("fake_session")
        return tok.value if tok and tok.value in self.sessions else None

    def _send(self, h, status, body: bytes | str, ctype="text/html; charset=utf-8", headers=None):
        b = body.encode() if isinstance(body, str) else body
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(b)

    def _json(self, h, data, status=200, headers=None):
        self._send(h, status, "while(1);" + json.dumps(data), "application/json; charset=utf-8", headers)

    def _redirect(self, h, loc, cookie=None):
        h.send_response(302)
        h.send_header("Location", loc)
        if cookie:
            h.send_header("Set-Cookie", cookie)
        h.send_header("Content-Length", "0")
        h.end_headers()

    def _paginate(self, h, items, path, q):
        try:
            per = min(int(q.get("per_page", [self.page_size])[0]), self.page_size)
        except ValueError:
            per = self.page_size
        page = int(q.get("page", ["1"])[0])
        chunk = items[(page - 1) * per: page * per]
        headers = {}
        if page * per < len(items):
            qq = {k: v for k, v in q.items() if k != "page"}
            qs = "&".join(f"{quote(k)}={quote(x)}" for k, vs in qq.items() for x in vs)
            headers["Link"] = f'<{self.origin}{path}?{qs}&page={page+1}>; rel="next"'
        self._json(h, [self._public(x) for x in chunk], headers=headers)

    @staticmethod
    def _public(x):
        if isinstance(x, dict):
            return {k: v for k, v in x.items() if not k.startswith("_")}
        return x

    # ---- routing -------------------------------------------------------------------------
    def _handle(self, h, method):
        parts = urlsplit(h.path)
        path, q = parts.path, parse_qs(parts.query)
        with self.lock:
            self.requests.append((method, h.path))
        if method != "GET" and path not in ("/login", "/login/mfa"):
            with self.lock:
                self.writes.append((method, h.path))
            return self._json(h, {"error": "fake canvas is read-only"}, 405)
        if path in ("/login", "/login/mfa"):
            return self._login(h, method, path, q)
        for prefix, st in self.fail_paths.items():
            if path.startswith(prefix):
                return self._json(h, {"errors": [{"message": "forced"}]}, st)
        sess = self._session(h)
        if path.startswith("/api/") and sess:
            with self.lock:
                self.api_requests += 1
                if self.expire_after_api_requests is not None and self.api_requests > self.expire_after_api_requests:
                    self.sessions.clear()
                    self.expire_after_api_requests = None
                    sess = None
        if path.startswith("/api/"):
            if not sess:
                return self._json(h, {"status": "unauthenticated", "errors": [{"message": "user authorization required"}]}, 401)
            return self._api(h, path, q)
        if not sess:
            return self._redirect(h, f"/login?return_to={quote(h.path)}")
        return self._html(h, path, q)

    def _login(self, h, method, path, q):
        if method == "GET" and path == "/login":
            rt = q.get("return_to", ["/"])[0]
            return self._send(h, 200, f"""<!doctype html><html><head><title>Log In to Fake Canvas</title></head><body>
                <h1>Example University Sign-in</h1>
                <form method="post" action="/login"><input type="hidden" name="return_to" value="{rt}">
                <label>Username <input id="username" name="username"></label>
                <label>Password <input id="password" type="password" name="password"></label>
                <button id="submit" type="submit">Sign in</button></form></body></html>""")
        length = int(h.headers.get("Content-Length") or 0)
        form = parse_qs(h.rfile.read(length).decode())
        if path == "/login":
            if form.get("username", [""])[0] == "astudent" and form.get("password", [""])[0] == PASSWORD:
                tok = secrets.token_hex(8)
                self.pending_mfa[tok] = form.get("return_to", ["/"])[0]
                return self._send(h, 200, f"""<!doctype html><html><body><h1>Two-step verification</h1>
                    <form method="post" action="/login/mfa"><input type="hidden" name="t" value="{tok}">
                    <label>Code <input id="code" name="code"></label><button id="verify" type="submit">Verify</button>
                    </form></body></html>""")
            return self._send(h, 401, "<html><body><p id='error'>Invalid credentials</p></body></html>")
        tok = form.get("t", [""])[0]
        if tok in self.pending_mfa and form.get("code", [""])[0] == MFA_CODE:
            rt = self.pending_mfa.pop(tok)
            sess = secrets.token_hex(16)
            with self.lock:
                self.sessions.add(sess)
            return self._redirect(h, rt if rt.startswith("/") else "/", cookie=f"fake_session={sess}; Path=/; HttpOnly")
        return self._send(h, 401, "<html><body><p id='error'>Invalid code</p></body></html>")

    def _html(self, h, path, q):
        if path == "/":
            return self._send(h, 200, "<html><head><title>Dashboard</title></head><body><div id='content'><h1>Dashboard</h1></div></body></html>")
        if path == "/courses":
            return self._send(h, 200, self._all_courses_html())
        m = re.match(r"^/courses/(\d+)", path)
        if m:
            return self._send(h, 200, f"<html><body><div id='content'><h1>Course {m.group(1)}</h1></div></body></html>")
        m = re.match(r"^/files/(\d+)/download$", path)
        if m:
            f = self.data["files"].get(m.group(1))
            if not f:
                return self._send(h, 404, "not found")
            return self._send(h, 200, f["_data"], f["content-type"])
        return self._send(h, 404, "<html><body>Not found</body></html>")

    def _all_courses_html(self) -> str:
        def rows(section):
            out = []
            for c in self.data["courses"].values():
                if c["section"] != section:
                    continue
                name = (f'<span class="name">{c["name"]}</span>' if c.get("unlinked")
                        else f'<a href="/courses/{c["id"]}"><span class="name">{c["name"]}</span></a>')
                out.append(f"""<tr class="course-list-table-row"><td class="course-list-star-column"></td>
                    <td class="course-list-course-title-column">{name}</td><td class="course-list-nickname-column"></td>
                    <td class="course-list-term-column">{c['term']['name']}</td>
                    <td class="course-list-enrolled-as-column">{c['role']}</td>
                    <td class="course-list-published-column">{'Yes' if c.get('published') else 'No'}</td></tr>""")
            return "".join(out)

        head = ("<thead><tr><th></th><th>Course</th><th>Nickname</th><th>Term</th><th>Enrolled as</th>"
                "<th>Published</th></tr></thead>")
        current = rows("current")
        past = rows("past")
        # Rendered asynchronously, like the real page's client-side table.
        return f"""<!doctype html><html><head><title>All Courses</title></head><body><div id="content">
          <h1>All Courses</h1><div id="tables"><p class="loading">Loading…</p></div></div>
          <script>
            setTimeout(function() {{
              document.getElementById('tables').innerHTML =
                '<h2>Current Enrollments</h2><table id="my_courses_table" class="course-list-table">{head}<tbody>' +
                {json.dumps(current)} + '</tbody></table>' +
                '<h2>Past Enrollments</h2><table id="past_enrollments_table" class="course-list-table">{head}<tbody>' +
                {json.dumps(past)} + '</tbody></table>';
            }}, {self.async_courses_ms});
          </script></body></html>"""

    def _course_json(self, c, q):
        d = {k: v for k, v in c.items() if k not in ("tabs", "section", "published", "role", "unlinked")}
        if "syllabus_body" not in q.get("include[]", []):
            d.pop("syllabus_body", None)
        if "teachers" not in q.get("include[]", []):
            d.pop("teachers", None)
        d["enrollments"] = [{"type": "student", "role": "StudentEnrollment", "enrollment_state": "active"}]
        return d

    def _api(self, h, path, q):
        D = self.data
        p = path[len("/api/v1"):]
        if p == "/users/self":
            return self._json(h, self.user)
        if p == "/users/self/profile":
            return self._json(h, self.profile)
        if p == "/users/self/groups":
            return self._paginate(h, D["groups"], path, q)
        if p == "/courses":
            lst = [self._course_json(c, q) for c in D["courses"].values() if not c.get("unlinked")]
            return self._paginate(h, lst, path, q)
        if p == "/calendar_events":
            codes = set(q.get("context_codes[]", []))
            typ = q.get("type", ["event"])[0]
            if typ == "assignment":
                evs = []
                for cid, lst in D["assignments"].items():
                    if f"course_{cid}" in codes:
                        for a in lst:
                            if a.get("due_at"):
                                evs.append({"id": f"assignment_{a['id']}", "title": a["name"], "start_at": a["due_at"],
                                            "end_at": a["due_at"], "context_code": f"course_{cid}",
                                            "assignment": {"id": a["id"]}, "html_url": a["html_url"]})
                return self._paginate(h, evs, path, q)
            return self._paginate(h, [e for e in D["calendar"] if e["context_code"] in codes], path, q)
        if p == "/conversations":
            return self._paginate(h, [{k: v for k, v in c.items() if k != "messages"} for c in D["conversations"]], path, q)
        m = re.match(r"^/conversations/(\d+)$", p)
        if m:
            c = next((x for x in D["conversations"] if x["id"] == m.group(1)), None)
            if not c:
                return self._json(h, {}, 404)
            if q.get("auto_mark_as_read", ["true"])[0] != "false":
                c["workflow_state"] = "read"
            return self._json(h, c)
        m = re.match(r"^/files/(\d+)$", p)
        if m:
            f = D["files"].get(m.group(1))
            if not f:
                return self._json(h, {"errors": [{"message": "not found"}]}, 404)
            d = self._public(f)
            d["url"] = f"{self.origin}/files/{f['id']}/download?verifier={secrets.token_hex(4)}"
            return self._json(h, d)
        m = re.match(r"^/folders/(\d+)/(files|folders)$", p)
        if m:
            fid, kind = m.groups()
            if kind == "files":
                return self._paginate(h, [self._file_public(f) for f in D["files"].values() if f["folder_id"] == fid], path, q)
            subs = [self._folder(f) for lst in D["folders"].values() for f in lst if f["parent_folder_id"] == fid]
            return self._paginate(h, subs, path, q)
        m = re.match(r"^/courses/(\d+)(/.*)?$", p)
        if not m:
            return self._json(h, {"errors": [{"message": "unknown endpoint"}]}, 404)
        cid, rest = m.group(1), m.group(2) or ""
        c = D["courses"].get(cid)
        if not c:
            return self._json(h, {}, 404)
        if c.get("unlinked"):
            return self._json(h, {"status": "unauthorized", "errors": [{"message": "unauthorized"}]}, 403)
        if rest == "":
            return self._json(h, self._course_json(c, q))
        if rest == "/tabs":
            labels = {"home": "Home", "announcements": "Announcements", "assignments": "Assignments", "grades": "Grades",
                      "discussions": "Discussions", "pages": "Pages", "files": "Files", "syllabus": "Syllabus",
                      "modules": "Modules", "quizzes": "Quizzes"}
            tabs = []
            for i, t in enumerate(c["tabs"], 1):
                if t.startswith("context_external_tool_"):
                    tabs.append({"id": t, "label": "Lecture Recordings", "type": "external", "position": i,
                                 "html_url": f"/courses/{cid}/external_tools/{t.rsplit('_', 1)[1]}"})
                else:
                    tabs.append({"id": t, "label": labels[t], "type": "internal", "position": i,
                                 "html_url": f"/courses/{cid}/{t}"})
            return self._json(h, tabs)
        if rest == "/front_page":
            fp = next((x for x in D["pages"].get(cid, []) if x["front_page"]), None)
            return self._json(h, fp) if fp else self._json(h, {}, 404)
        if rest == "/pages":
            return self._paginate(h, [{k: v for k, v in x.items() if k != "body"} for x in D["pages"].get(cid, [])], path, q)
        m2 = re.match(r"^/pages/(.+)$", rest)
        if m2:
            key = unquote(m2.group(1))
            pg = next((x for x in D["pages"].get(cid, []) if x["url"] == key or f"page_id:{x['page_id']}" == key), None)
            return self._json(h, pg) if pg else self._json(h, {"errors": [{"message": "page not found"}]}, 404)
        if rest == "/discussion_topics":
            lst = D["announcements"].get(cid, []) if q.get("only_announcements", [""])[0] == "true" else D["discussions"].get(cid, [])
            return self._paginate(h, lst, path, q)
        m2 = re.match(r"^/discussion_topics/(\d+)(/view)?$", rest)
        if m2:
            tid, view = m2.groups()
            t = next((x for x in D["announcements"].get(cid, []) + D["discussions"].get(cid, []) if x["id"] == tid), None)
            if not t:
                return self._json(h, {}, 404)
            if view:
                if t.get("_view_forbidden"):
                    return self._json(h, {"status": "unauthorized"}, 403)
                return self._json(h, t.get("_view", {"participants": [], "view": []}))
            return self._json(h, t)
        if rest == "/assignment_groups":
            return self._paginate(h, D["assignment_groups"].get(cid, []), path, q)
        if rest == "/assignments":
            inc = "submission" in q.get("include[]", [])
            out = []
            for a in D["assignments"].get(cid, []):
                d = copy.deepcopy(a)
                if not inc:
                    d.pop("submission", None)
                out.append(d)
            return self._paginate(h, out, path, q)
        m2 = re.match(r"^/assignments/(\d+)$", rest)
        if m2:
            a = next((x for x in D["assignments"].get(cid, []) if x["id"] == m2.group(1)), None)
            return self._json(h, a) if a else self._json(h, {}, 404)
        if rest == "/students/submissions":
            subs = []
            for a in D["assignments"].get(cid, []):
                s = dict(a.get("submission") or {})
                s["assignment_id"] = a["id"]
                if "submission_comments" in q.get("include[]", []):
                    s["submission_comments"] = D["comments"].get(a["id"], [])
                if "rubric_assessment" in q.get("include[]", []) and a["id"] == "2001":
                    s["rubric_assessment"] = {"crit1": {"points": 18, "comments": "Clear"}}
                subs.append(s)
            return self._paginate(h, subs, path, q)
        if rest == "/enrollments":
            g = D["enrollments"].get(cid)
            return self._paginate(h, [{"type": "StudentEnrollment", "user_id": USER["id"], "grades": g}] if g else [], path, q)
        if rest == "/quizzes":
            return self._paginate(h, D["quizzes"].get(cid, []), path, q)
        m2 = re.match(r"^/quizzes/(\d+)$", rest)
        if m2:
            qz = next((x for x in D["quizzes"].get(cid, []) if x["id"] == m2.group(1)), None)
            return self._json(h, qz) if qz else self._json(h, {}, 404)
        if rest == "/modules":
            mods = []
            for mo in D["modules"].get(cid, []):
                d = {k: v for k, v in mo.items() if not k.startswith("_")}
                if d.get("items") is None:
                    d.pop("items", None)
                    d["items_url"] = f"{self.origin}/api/v1/courses/{cid}/modules/{mo['id']}/items"
                mods.append(d)
            return self._paginate(h, mods, path, q)
        m2 = re.match(r"^/modules/(\d+)(/items)?$", rest)
        if m2:
            mo = next((x for x in D["modules"].get(cid, []) if x["id"] == m2.group(1)), None)
            if not mo:
                return self._json(h, {}, 404)
            if m2.group(2):
                return self._paginate(h, mo.get("items") or mo.get("_items") or [], path, q)
            return self._json(h, self._public(mo))
        if rest == "/folders":
            return self._paginate(h, [self._folder(f) for f in D["folders"].get(cid, [])], path, q)
        m2 = re.match(r"^/folders/by_path/?(.*)$", rest)
        if m2:
            want = unquote(m2.group(1)).strip("/")
            chain = [f for f in D["folders"].get(cid, []) if want.startswith(f["full_name"].split("/", 1)[-1]) or f["parent_folder_id"] is None]
            target = next((f for f in D["folders"].get(cid, []) if f["full_name"] == "course files/" + want or (not want and f["parent_folder_id"] is None)), None)
            if not target:
                return self._json(h, {}, 404)
            return self._json(h, [self._folder(f) for f in chain if f is not target] + [self._folder(target)])
        m2 = re.match(r"^/files/(\d+)$", rest)
        if m2:
            return self._api(h, f"/api/v1/files/{m2.group(1)}", q)
        return self._json(h, {"errors": [{"message": "unknown endpoint"}]}, 404)

    def _file_public(self, f):
        d = self._public(f)
        d["url"] = f"{self.origin}/files/{f['id']}/download?verifier={secrets.token_hex(4)}"
        return d

    def _folder(self, f):
        D = self.data
        files = [x for x in D["files"].values() if x["folder_id"] == f["id"]]
        subs = [x for lst in D["folders"].values() for x in lst if x["parent_folder_id"] == f["id"]]
        return {**f, "files_count": len(files), "folders_count": len(subs), "updated_at": "2026-09-01T00:00:00Z",
                "locked": False, "hidden": False, "locked_for_user": False}


def main():
    ap = argparse.ArgumentParser(description="Run the synthetic Canvas server")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    fc = FakeCanvas(port=args.port)
    print(f"Fake Canvas at {fc.origin}  (username: astudent  password: {PASSWORD!r}  MFA code: {MFA_CODE})")
    fc.httpd.serve_forever()


if __name__ == "__main__":
    main()
