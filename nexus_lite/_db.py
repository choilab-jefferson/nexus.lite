"""
nexus_lite._db — workspace path + SQLite WAL connection + schema bootstrap.

Shared by the memory tiers (stm/mtm/ltm) and the task/session code in core. Stdlib
only; imports no nexus_lite module at import time except the stdlib-only `artifacts`
(private mkdir helper).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Iterator, Optional, Union

from .artifacts import mkdir_private


def get_workspace() -> Path:
    """Return canonical workspace root Path (.nexus/ or NEXUS_LITE_DIR or NEXUS_LITE_HOME)."""
    env = os.environ.get("NEXUS_LITE_DIR") or os.environ.get("NEXUS_LITE_HOME")
    if env:
        return Path(env).resolve()
    return Path(".nexus").resolve()


def get_db_path() -> Path:
    """Return path to SQLite WAL database."""
    return get_workspace() / "lite.db"


def get_bus_dir() -> Path:
    """The file bus directory (source of truth for messages, cursors, sessions): $NEXUS_BUS_DIR, else
    <workspace>/bus. lite.db is only its local index (docs/specs/file-bus.md)."""
    env = os.environ.get("NEXUS_BUS_DIR")
    return Path(env).resolve() if env else get_workspace() / "bus"


def mark_session(name: str, live: bool) -> None:
    """Best-effort: flip an EXISTING sessions-registry row live/unregistered.

    Update-only and never creates the workspace/DB: a socket with no row already counts as
    live on the bus, so only a stale 'unregistered' row (or a kill) needs a write. Called by
    pty.spawn / pty.kill (edge pty -> _db; _db imports only stdlib-only artifacts, so no cycle).
    """
    try:  # the session file (source of truth) first; update-only, never creates the bus
        from . import filebus

        bd = get_bus_dir()
        if (bd / "VERSION").is_file():
            filebus.open_bus(bd).set_session_live(name, live)
    except Exception:
        pass
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
    """v1 -> v2: messages.seq (backfilled), bus_cursors, bus_meta, sessions.last_seen."""
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


def _m3_bus_consumers(con: sqlite3.Connection) -> None:
    """v2 -> v3: bus_cursors PK (target) -> (target, consumer); old rows become consumer ''."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(bus_cursors);")}
    if "consumer" in cols:
        return
    con.execute("ALTER TABLE bus_cursors RENAME TO bus_cursors_old;")
    con.execute(
        "CREATE TABLE bus_cursors (target TEXT NOT NULL, consumer TEXT NOT NULL DEFAULT '', "
        "acked_seq INTEGER NOT NULL DEFAULT 0, updated REAL, PRIMARY KEY (target, consumer));"
    )
    con.execute(
        "INSERT INTO bus_cursors (target, consumer, acked_seq, updated) "
        "SELECT target, '', acked_seq, updated FROM bus_cursors_old;"
    )
    con.execute("DROP TABLE bus_cursors_old;")


def _m4_cas_objects(con: sqlite3.Connection) -> None:
    """v3 -> v4: index of content-addressed blobs (bytes live under .nexus/cas)."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS cas_objects (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, "
        "created_at REAL NOT NULL, first_task_id TEXT);"
    )


def _m5_file_bus(con: sqlite3.Connection) -> None:
    """v4 -> v5: files become the bus source of truth (docs/specs/file-bus.md section 9). Adds messages.fid (the
    file id) and exports the legacy bus into the bus dir: every message not acked by all cursors becomes
    msg/<target>/<id>.json under a deterministic id (a retried migration rewrites the same files), live and
    unregistered sessions become sess/<name>.json, and each cursor becomes cur/<target>/<consumer>.json with the hwm
    of the last exported id at or below its acked_seq. Exported rows keep their legacy id and seq and gain `fid`."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages);")}
    if "fid" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN fid TEXT;")
    con.execute("CREATE INDEX IF NOT EXISTS idx_messages_fid ON messages(fid);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_messages_target_fid ON messages(target, fid);")
    from . import filebus  # lazy: stdlib-only module, imports no tier

    rows = con.execute(
        "SELECT id, sender, target, kind, summary, content, timestamp, seq FROM messages "
        "WHERE fid IS NULL ORDER BY seq, rowid;").fetchall()
    sessions = con.execute("SELECT name, status, created_at, last_seen FROM sessions;").fetchall()
    cursors = con.execute("SELECT target, consumer, acked_seq FROM bus_cursors;").fetchall()
    if not rows and not sessions and not cursors:
        return
    bus = filebus.open_bus(get_bus_dir())
    acked = {}  # target -> [acked_seq of each cursor]
    for c in cursors:
        acked.setdefault(c["target"], []).append(c["acked_seq"] or 0)
    every = [a for v in acked.values() for a in v]
    exported = []  # (old seq, new file id, row)
    prev_ms = 0
    for r in rows:
        try:
            tgt = r["target"]
            filebus.check_name("target", tgt)
            if r["seq"] is None:
                continue
            curs = every if tgt == filebus.ALL else acked.get(tgt, [])
            if curs and all(a >= r["seq"] for a in curs):
                continue  # acked by every cursor: stays a legacy index row (read history)
            ms = max(int((r["timestamp"] or 0) * 1000), prev_ms + 1)
            prev_ms = ms
            wid = hashlib.sha1(("legacy:" + str(r["id"])).encode()).hexdigest()[:8]
            fid = f"m{ms:013d}-{wid}-{len(exported) % 1000000:06d}"
            bus.publish_message(
                tgt, fid, sender=r["sender"] or "user", kind=r["kind"] or "dm", summary=r["summary"] or "",
                body=r["content"] or r["summary"] or "", timestamp=r["timestamp"],
                meta={"legacy_id": r["id"], "origin": "lite-legacy"})
            exported.append((r["seq"], fid, r))
        except (ValueError, TypeError, OSError):
            continue  # an unexportable legacy row (no target, bad name) stays an index-only row
    for _seq, fid, r in exported:
        con.execute("UPDATE messages SET fid = ? WHERE id = ?;", (fid, r["id"]))
    for c in cursors:
        try:
            filebus.check_name("target", c["target"])
            cons = c["consumer"] or None
            if bus.read_cursor(c["target"], cons)["exists"]:
                continue  # a newer cursor file wins (another host migrated first)
            ok = [fid for seq, fid, _r in exported if seq <= (c["acked_seq"] or 0)
                  and (c["target"] == filebus.ALL or _r["target"] in (c["target"], filebus.ALL))]
            bus.ack(c["target"], cons, ok[-1:])
        except (ValueError, TypeError, OSError):
            continue
    for s in sessions:
        try:
            if s["status"] not in ("live", "unregistered") or bus.read_session(s["name"]) is not None:
                continue
            bus.import_session(s["name"], s["status"], s["created_at"], s["last_seen"])
        except (ValueError, TypeError, OSError):
            continue


def _m6_message_data(con: sqlite3.Connection) -> None:
    """v5 -> v6: messages.data (JSON text of the structured payload of typed kinds such as `job`)."""
    if "data" not in {r[1] for r in con.execute("PRAGMA table_info(messages);")}:
        con.execute("ALTER TABLE messages ADD COLUMN data TEXT;")


MIGRATIONS = [_m1_stamp_baseline, _m2_bus_v1, _m3_bus_consumers, _m4_cas_objects, _m5_file_bus, _m6_message_data]  # index i migrates v<i> -> v<i+1>
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

    for d in (ws, ws / "inbox", ws / "artifacts", ws / "wiki"):
        mkdir_private(d)  # workspace holds prompts, outputs and messages: owner-only (0700)
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
