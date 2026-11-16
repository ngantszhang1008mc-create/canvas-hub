"""Register the synthetic external sites as links of the fixture course (tests and the offline demo)."""

from __future__ import annotations

from ..util import now_iso, sha256_text

LINKS = (
    ("course", "COMP course website", "course", "/old/"),
    ("notes", "Lecture notes (PDF)", "course", "/comp/notes.pdf"),
    ("grader", "Grader: assignments", "grader", "/assignments"),
    ("grades", "Grader: grades", "grader", "/grades"),
    ("forum", "Class forum", "forum", "/class/abc123"),
    ("syllabus", "Public syllabus", "course", "/public/syllabus"),
    ("missing", "Old tutorial page", "course", "/missing"),
)


def add_link(hub, account_id: int, url: str, title: str, course_id: str = "4242") -> int:
    """Insert a link row directly.

    The app's own ``register`` refuses loopback addresses and unusual ports,
    which is right for real use and exactly what the synthetic sites need.
    """
    with hub.db.tx() as c:
        c.execute("INSERT INTO external_sources(account_id,course_id,url,url_key,title,status,last_seen_at) VALUES (?,?,?,?,?,?,?)",
                  (account_id, course_id, url, sha256_text(url), title, "unclassified", now_iso()))
        return c.execute("SELECT id FROM external_sources WHERE account_id=? AND course_id=? AND url_key=?",
                         (account_id, course_id, sha256_text(url))).fetchone()["id"]


def add_links(hub, account_id: int, sites, names=None) -> dict[str, int]:
    return {key: add_link(hub, account_id, sites.url(site, path), title)
            for key, title, site, path in LINKS if names is None or key in names}
