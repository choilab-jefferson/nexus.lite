"""
nexus_lite._db — workspace path + SQLite WAL connection + schema bootstrap.

Shared by the memory tiers (stm/mtm/ltm) and the task/session code in core. Stdlib
only; imports no other nexus_lite module at import time.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterator, Optional, Union


def get_workspace() -> Path:
    """Return canonical workspace root Path (.nexus/ or NEXUS_LITE_DIR or NEXUS_LITE_HOME)."""
    env = os.environ.get("NEXUS_LITE_DIR") or os.environ.get("NEXUS_LITE_HOME")
    if env:
        return Path(env).resolve()
    return Path(".nexus").resolve()


def get_db_path() -> Path:
    """Return path to SQLite WAL database."""
    return get_workspace() / "lite.db"


def mark_session(name: str, live: bool) -> None:
    """Best-effort: flip an EXISTING sessions-registry row live/unregistered (lite#8).

    Update-only and never creates the workspace/DB: a socket with no row already counts as
    live on the bus, so only a stale 'unregistered' row (or a kill) needs a write. Called by
    pty.spawn / pty.kill (edge pty -> _db; _db imports no lite module, so no cycle).
    """
    try:
        if not get_db_path().exists():
            return
        with connect() as con:
            if live:
                con.execute(
                    "UPDATE sessions SET status = 'live', last_seen = strftime('%s','now') "
                    "WHERE name = ?;", (name,))
            else:
                con.execute("UPDATE sessions SET status = 'unregistered' WHERE name = ?;", (name,))
    except Exception:
        pass


def get_db() -> sqlite3.Connection:
    """Open SQLite connection configured with WAL journal mode and Row factory.

    The caller owns the connection and MUST close it (``with con:`` only commits). Prefer
    :func:`connect`, which commits/rolls back and closes.
    """
    db_p = get_db_path()
    db_p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_p), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection; commit on success, roll back on error, always close."""
    con = get_db()
    try:
        with con:
            yield con
    finally:
        con.close()


# --- schema versioning (PRAGMA user_version) -------------------------------------------
# v0 = any DB without user_version (the original CREATE TABLE IF NOT EXISTS schema).
# Table/column names are frozen. Append new steps; never edit old ones.


def _m1_stamp_baseline(con: sqlite3.Connection) -> None:
    """v0 -> v1: baseline schema (created idempotently by _ensure_base_schema); stamp only."""


def _m2_bus_v1(con: sqlite3.Connection) -> None:
    """v1 -> v2 (lite#8): messages.seq (backfilled), bus_cursors, bus_meta, sessions.last_seen."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages);")}
    if "seq" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN seq INTEGER;")
    rows = con.execute("SELECT rowid FROM messages ORDER BY timestamp, rowid;").fetchall()
    for i, r in enumerate(rows, 1):
        con.execute("UPDATE messages SET seq = ? WHERE rowid = ?;", (i, r[0]))
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_seq ON messages(seq);")
    con.execute(
        "CREATE TABLE IF NOT EXISTS bus_cursors ("
        "target TEXT PRIMARY KEY, acked_seq INTEGER NOT NULL DEFAULT 0, updated REAL);"
    )
    con.execute("CREATE TABLE IF NOT EXISTS bus_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);")
    con.execute("INSERT OR REPLACE INTO bus_meta (key, value) VALUES ('last_seq', ?);", (len(rows),))
    scols = {r[1] for r in con.execute("PRAGMA table_info(sessions);")}
    if "last_seen" not in scols:
        con.execute("ALTER TABLE sessions ADD COLUMN last_seen REAL;")


MIGRATIONS = [_m1_stamp_baseline, _m2_bus_v1]  # index i migrates v<i> -> v<i+1>
SCHEMA_VERSION = len(MIGRATIONS)

_BASE_DDL = (
    """CREATE TABLE IF NOT EXISTS sessions (
        name TEXT PRIMARY KEY, sock TEXT, cmd TEXT, created_at REAL, status TEXT);""",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, sender TEXT, target TEXT, kind TEXT, summary TEXT,
        path TEXT, content TEXT, timestamp REAL);""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, caller TEXT, agent_cmd TEXT, status TEXT, rc INTEGER,
        created_at REAL, completed_at REAL, artifact_dir TEXT);""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(slug, content);",
)


def migrate(con: sqlite3.Connection) -> int:
    """Bring `con` to SCHEMA_VERSION inside one transaction; refuse a newer DB."""
    cur = con.execute("PRAGMA user_version;").fetchone()[0]
    if cur > SCHEMA_VERSION:
        raise RuntimeError(
            f"lite.db schema version {cur} is newer than this nexus_lite supports "
            f"({SCHEMA_VERSION}); upgrade nexus_lite"
        )
    if cur == SCHEMA_VERSION:
        return cur
    con.execute("BEGIN IMMEDIATE;")
    try:
        cur = con.execute("PRAGMA user_version;").fetchone()[0]  # re-read under the write lock
        if cur > SCHEMA_VERSION:
            raise RuntimeError(f"lite.db schema version {cur} is newer than {SCHEMA_VERSION}")
        for ddl in _BASE_DDL:
            con.execute(ddl)
        for i in range(cur, SCHEMA_VERSION):
            MIGRATIONS[i](con)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
        con.execute("COMMIT;")
    except BaseException:
        con.execute("ROLLBACK;")
        raise
    return SCHEMA_VERSION


_INIT_DONE: set[str] = set()
_INIT_LOCK = threading.Lock()


def reset_init() -> None:
    """Forget memoized init() state (tests / explicit re-init)."""
    with _INIT_LOCK:
        _INIT_DONE.clear()


def init(
    workspace: Optional[Union[str, Path]] = None,
    root: Optional[Union[str, Path]] = None,
    force: bool = False,
) -> Path:
    """Initialize workspace layout and SQLite schema; once per (process, db path).

    `force=True` (or :func:`reset_init`) re-runs it.
    """
    target = workspace or root
    if target:
        ws = Path(target).resolve()
        os.environ["NEXUS_LITE_HOME"] = str(ws)
        os.environ["NEXUS_LITE_DIR"] = str(ws)
    else:
        ws = get_workspace()

    key = str(get_db_path())
    if not force and key in _INIT_DONE and Path(key).exists() and (ws / "inbox").is_dir():
        return ws

    ws.mkdir(parents=True, exist_ok=True)
    (ws / "inbox").mkdir(parents=True, exist_ok=True)
    (ws / "artifacts").mkdir(parents=True, exist_ok=True)
    (ws / "wiki").mkdir(parents=True, exist_ok=True)
    try:
        from .memory import stm as _stm  # lazy: keeps _db import-free of the tiers

        _stm.ensure_project_node(ws)
    except Exception:
        pass

    with contextlib.closing(get_db()) as con:
        migrate(con)
    with _INIT_LOCK:
        _INIT_DONE.add(key)
    return ws
