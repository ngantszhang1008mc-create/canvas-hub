"""SQLite connection handling and migrations."""

from __future__ import annotations

import sqlite3
import threading
import weakref
from contextlib import contextmanager
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Iterator

from .util import now_iso


def _migration_files() -> list[tuple[int, str, str]]:
    out = []
    pkg = importlib_resources.files("canvas_hub") / "migrations"
    for entry in pkg.iterdir():
        name = entry.name
        if not name.endswith(".sql"):
            continue
        version = int(name.split("_", 1)[0])
        out.append((version, name, entry.read_text(encoding="utf-8")))
    return sorted(out)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection) -> list[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    done = []
    for version, name, sql in _migration_files():
        if version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would COMMIT implicitly; run statements one by one.
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                (version, name, now_iso()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        done.append(version)
    return done


def _split_sql(sql: str) -> list[str]:
    stmts, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";") and sqlite3.complete_statement("\n".join(buf)):
            stmts.append("\n".join(buf))
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


class _ThreadConnection:
    """Owned by thread-local storage; released when the worker exits."""

    def __init__(self, conn):
        self.conn = conn
        self.owner = weakref.ref(threading.current_thread())

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __del__(self):
        self.close()


class Database:
    """Thread-aware wrapper: one connection per thread, explicit transactions."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._all = weakref.WeakSet()
        self._lock = threading.Lock()
        migrate(self.conn)

    @property
    def conn(self) -> sqlite3.Connection:
        holder = getattr(self._local, "holder", None)
        if holder is None or holder.conn is None:
            holder = _ThreadConnection(connect(self.path))
            self._local.holder = holder
            with self._lock:
                self._all.add(holder)
        return holder.conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """IMMEDIATE transaction; nested use joins the outer transaction."""
        c = self.conn
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield c
            finally:
                self._local.depth -= 1
            return
        c.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        self._local.after_commit = []
        callbacks = []
        try:
            yield c
            c.execute("COMMIT")
            callbacks = self._local.after_commit
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            self._local.depth = 0
            self._local.after_commit = []
        # Release the SQLite write lock before callbacks acquire service locks.
        # A callback can start another transaction without replacing this queue.
        for callback in callbacks:
            callback()

    def after_commit(self, callback) -> None:
        """Run once after the outer transaction commits; discard on rollback.

        Import hooks may be nested inside atomic adapter publication. Scheduling
        them here preserves that publication while avoiding DB/service lock
        inversion. Outside a transaction, preserve synchronous hook behavior.
        """
        if getattr(self._local, "depth", 0):
            self._local.after_commit.append(callback)
        else:
            callback()

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            for holder in list(self._all):
                owner = holder.owner()
                # Never close a connection while another live thread may use it.
                # That thread's local storage releases it on thread exit.
                if owner is not None and owner is not threading.current_thread() and owner.is_alive():
                    continue
                try:
                    holder.close()
                except Exception:  # noqa: BLE001
                    pass
                self._all.discard(holder)
