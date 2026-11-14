"""
nexus_lite.memory.mtm — MTM: file/SQLite message bus with inbox pointer delivery.

MTM = flow carried across interruptions; its transport is this bus (nexus wording; MTM+ is retired).
`bus_send` is the lite verb for what nexus calls say/dm/relay; target `ALL` = broadcast to live sessions.

A message is an inbox file `<ws>/inbox/<id>.md` + a row in the `messages` table; a live
PTY session addressed by the message gets a pointer line injected. Edges: `.._db`
(workspace/SQLite), `..protocol` (pointer-line format), `..pty` (live session delivery).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from .. import pty
from ..artifacts import mkdir_private, replace_private, write_private
from .._db import connect, get_workspace, init
from ..protocol import format_pointer_line, validate_bus_field


# A registered session counts as live while last_seen is newer than this (seconds).
LIVE_TTL_S = 300.0

_COLS = "id, sender, target, kind, summary, path, content, timestamp, seq"

# Under NEXUS_SENSITIVITY=phi a `task`/`handoff` message may only carry a pointer-shaped
# one-liner (`TASK_COMPLETE <id>: <status>`); anything else could be agent output and is redacted.
_PHI_KINDS = ("task", "handoff")
_PHI_SAFE_RE = re.compile(r"^(TASK_COMPLETE|TASK_ESCALATE|HANDOFF)[\w .:\-]{0,200}$")
PHI_REDACTED = "[phi-redacted]"


def _phi_safe_message(kind: str, message: str) -> str:
    if os.environ.get("NEXUS_SENSITIVITY") == "phi" and kind in _PHI_KINDS:
        if not _PHI_SAFE_RE.match(message.strip()):
            return PHI_REDACTED
    return message


def _registered_live(con) -> set[str]:
    cut = time.time() - LIVE_TTL_S
    rows = con.execute(
        "SELECT name FROM sessions WHERE status = 'live' AND last_seen >= ?;", (cut,)
    ).fetchall()
    return {r["name"] for r in rows}


def _live_names(con) -> set[str]:
    """Bus-live sessions. A dtach session whose socket probes alive is live regardless of
    last_seen (no heartbeat needed), unless its registry row says status != 'live'
    (bus_unregister / pty.kill). A live socket with NO row is live too, so sessions created
    before the registry existed (upgrade) still receive ALL. Rows without a socket are live
    only while status='live' and last_seen is within LIVE_TTL_S."""
    rows = con.execute("SELECT name, status FROM sessions;").fetchall()
    status = {r["name"]: r["status"] for r in rows}
    live = _registered_live(con)
    for s in pty.list_sessions():
        if status.get(s["name"], "live") == "live":
            live.add(s["name"])
    return live


def _inject(name: str, pointer: str) -> bool:
    try:
        pty.send(name, pointer, enter=True)
        return True
    except Exception:
        return False


def bus_send(
    target: str,
    message: str,
    sender: str = "user",
    kind: str = "dm",
) -> dict:
    """
    Send a message via the message bus.
    Writes .nexus/inbox/<msg_id>.md, logs to SQLite messages table (with a monotonic integer
    `seq`), and injects '[nexus] <kind> <msg_id> <path> - <summary>' into the target's PTY.
    ALL fans out to every bus-live session (see _live_names): live dtach sockets + fresh registrations.
    """
    validate_bus_field("target", target)
    validate_bus_field("sender", sender)
    validate_bus_field("kind", kind)
    if not isinstance(message, str):
        raise TypeError(f"message must be str, got {type(message).__name__}")
    message = _phi_safe_message(kind, message)
    init()
    ws = get_workspace()
    mkdir_private(ws / "inbox")

    msg_id = f"m-{uuid.uuid4().hex[:8]}"
    lines = [ln.strip() for ln in message.strip().splitlines() if ln.strip()]
    summary = lines[0][:100] if lines else ""
    ts = time.time()
    path = ws / "inbox" / f"{msg_id}.md"

    md_content = f"""---
id: {msg_id}
sender: {sender}
target: {target}
kind: {kind}
timestamp: {ts}
---
{message}
"""
    replace_private(path, md_content)  # readers never see a half-written body

    with connect() as con:
        con.execute("BEGIN IMMEDIATE;")  # serialise seq allocation across processes
        seq = con.execute("SELECT value FROM bus_meta WHERE key = 'last_seq';").fetchone()[0] + 1
        con.execute("UPDATE bus_meta SET value = ? WHERE key = 'last_seq';", (seq,))
        con.execute(
            """
            INSERT INTO messages (id, sender, target, kind, summary, path, content, timestamp, seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (msg_id, sender, target, kind, summary, str(path), message, ts, seq),
        )
        live = _live_names(con)

    pointer = format_pointer_line(kind, msg_id, str(path), summary)

    live_sessions = pty.list_sessions()
    if target.upper() == "ALL":
        for s in live_sessions:
            if s["name"] in live:
                _inject(s["name"], pointer)
    else:
        for s in live_sessions:
            if s["name"] == target or s["sock"] == target:
                _inject(s["name"], pointer)

    return {
        "id": msg_id,
        "seq": seq,
        "sender": sender,
        "target": target,
        "kind": kind,
        "summary": summary,
        "path": str(path),
        "timestamp": ts,
    }


def bus_read(
    target: Optional[str] = None,
    limit: int = 20,
    since_seq: Optional[int] = None,
    unacked: bool = False,
    consumer: Optional[str] = None,
) -> list[dict]:
    """Read messages, optionally filtered by target.

    Default: newest first (unchanged). With `since_seq` and/or `unacked` (needs `target`):
    only seq > max(since_seq, target's acked cursor), oldest first. `consumer` selects an
    independent cursor (e.g. "pty" vs "turn"); None = the default cursor.
    """
    init()
    if target is not None:
        validate_bus_field("target", target)
    if unacked and not target:
        raise ValueError("unacked requires target")
    with connect() as con:
        if since_seq is None and not unacked:
            if target:
                cur = con.execute(
                    f"SELECT {_COLS} FROM messages WHERE target = ? OR target = 'ALL' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (target, limit),
                )
            else:
                cur = con.execute(
                    f"SELECT {_COLS} FROM messages ORDER BY timestamp DESC LIMIT ?", (limit,)
                )
            return [dict(r) for r in cur.fetchall()]
        floor = int(since_seq or 0)
        if unacked:
            floor = max(floor, _acked(con, target, consumer))
        if target:
            cur = con.execute(
                f"SELECT {_COLS} FROM messages WHERE seq > ? AND (target = ? OR target = 'ALL') "
                "ORDER BY seq LIMIT ?",
                (floor, target, limit),
            )
        else:
            cur = con.execute(
                f"SELECT {_COLS} FROM messages WHERE seq > ? ORDER BY seq LIMIT ?", (floor, limit)
            )
        return [dict(r) for r in cur.fetchall()]


def _acked(con, target: str, consumer: Optional[str] = None) -> int:
    row = con.execute(
        "SELECT acked_seq FROM bus_cursors WHERE target = ? AND consumer = ?;", (target, consumer or "")
    ).fetchone()
    return row["acked_seq"] if row else 0


def _floor(con, target: str) -> int:
    """Lowest cursor in use for `target` across its consumers (0 when none has acked yet):
    a message above it is unread by at least one consumer."""
    row = con.execute("SELECT MIN(acked_seq) FROM bus_cursors WHERE target = ?;", (target,)).fetchone()
    return row[0] or 0


def bus_ack(target: str, seq: int, consumer: Optional[str] = None) -> int:
    """Advance `target`'s cursor (for `consumer`; None = default) to `seq`, never back.
    Returns the new acked_seq."""
    validate_bus_field("target", target)
    if consumer:
        validate_bus_field("consumer", consumer)
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise TypeError(f"seq must be int, got {type(seq).__name__}")
    init()
    with connect() as con:
        con.execute("BEGIN IMMEDIATE;")
        new = max(_acked(con, target, consumer), seq)
        con.execute(
            "INSERT INTO bus_cursors (target, consumer, acked_seq, updated) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(target, consumer) DO UPDATE SET acked_seq = excluded.acked_seq, updated = excluded.updated;",
            (target, consumer or "", new, time.time()),
        )
    return new


def bus_watch(
    target: str,
    *,
    consumer: str = "watch",
    poll_s: float = 0.5,
    timeout: Optional[float] = None,
    ack: bool = True,
    callback=None,
    stop=None,
):
    """Poll `target`'s inbox and deliver new messages oldest-first via an independent cursor.

    Generator by default (ends after `timeout` seconds, or when `stop.is_set()`); with
    `callback` it runs the loop itself, calls `callback(msg)` per message and returns the
    count. `consumer` (default "watch") keeps its own cursor, so default/"turn" are untouched.
    `ack=True` advances the cursor after the message is handled (at-least-once); `ack=False`
    leaves it, so a later watch re-yields. SQLite polling, no threads.
    """
    if callback is None:
        return _watch_iter(target, consumer, poll_s, timeout, ack, stop)
    n = 0
    for m in _watch_iter(target, consumer, poll_s, timeout, ack, stop):
        callback(m)
        n += 1
    return n


def _watch_iter(target, consumer, poll_s, timeout, ack, stop):
    deadline = None if timeout is None else time.monotonic() + timeout
    # Without ack the cursor never moves; track it locally so one watch yields each message once.
    seen = 0
    while True:
        if stop is not None and stop.is_set():
            return
        msgs = [
            m for m in bus_read(target=target, unacked=True, consumer=consumer, limit=100)
            if m["seq"] > seen
        ]
        for m in msgs:
            seen = m["seq"]
            yield m
            if ack:
                bus_ack(target, m["seq"], consumer=consumer)
        if msgs:
            continue  # drain backlog before sleeping
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(poll_s, left))
        else:
            time.sleep(poll_s)


def bus_register(name: str) -> dict:
    """Mark session `name` live (refreshes last_seen). On a not-live -> live transition, re-emit
    the pointer for each direct message unread by any consumer cursor in use (lowest cursor wins; no
    cursor = everything). Already-live re-register = heartbeat only."""
    validate_bus_field("name", name)
    init()
    now = time.time()
    with connect() as con:
        con.execute("BEGIN IMMEDIATE;")
        was_live = name in _registered_live(con)  # registry only: a bare socket is not 'registered'
        con.execute(
            "INSERT INTO sessions (name, created_at, status, last_seen) VALUES (?, ?, 'live', ?) "
            "ON CONFLICT(name) DO UPDATE SET status = 'live', last_seen = excluded.last_seen;",
            (name, now, now),
        )
        pending = []
        if not was_live:
            pending = con.execute(
                f"SELECT {_COLS} FROM messages WHERE target = ? AND seq > ? ORDER BY seq;",
                (name, _floor(con, name)),
            ).fetchall()
    for m in pending:
        _inject(name, format_pointer_line(m["kind"], m["id"], m["path"], m["summary"]))
    return {"name": name, "redelivered": [m["id"] for m in pending]}


def bus_unregister(name: str) -> bool:
    """Mark session `name` not live. Returns True if it was registered."""
    validate_bus_field("name", name)
    init()
    with connect() as con:
        cur = con.execute("UPDATE sessions SET status = 'unregistered' WHERE name = ?;", (name,))
        return cur.rowcount > 0


def bus_prune(max_age_s: float, keep_last: int = 0) -> int:
    """Delete messages older than `max_age_s` that are acked (direct: every consumer cursor of the target >= seq;
    ALL: every cursor >= seq and every live registered session has a cursor), never the newest `keep_last`. Removes inbox files under the
    workspace inbox only. Returns the number of messages deleted."""
    if not isinstance(max_age_s, (int, float)) or not isinstance(keep_last, int):
        raise TypeError("max_age_s must be a number and keep_last an int")
    init()
    inbox = (get_workspace() / "inbox").resolve()
    cutoff = time.time() - max_age_s
    with connect() as con:
        con.execute("BEGIN IMMEDIATE;")
        floor_all = con.execute("SELECT MIN(acked_seq) FROM bus_cursors;").fetchone()[0] or 0
        # A live registered session with no cursor row has read nothing: it pins ALL messages.
        if con.execute(
            "SELECT 1 FROM sessions s WHERE s.status = 'live' "
            "AND NOT EXISTS (SELECT 1 FROM bus_cursors c WHERE c.target = s.name) LIMIT 1;"
        ).fetchone():
            floor_all = 0
        top = con.execute("SELECT MAX(seq) FROM messages;").fetchone()[0] or 0
        keep_from = top - max(keep_last, 0)  # seq > keep_from are kept
        rows = con.execute(
            """
            SELECT m.id, m.path FROM messages m
            WHERE m.timestamp < ? AND m.seq <= ?
              AND ((m.target = 'ALL' AND m.seq <= ?) OR (m.target != 'ALL' AND m.seq <= COALESCE(
                   (SELECT MIN(c.acked_seq) FROM bus_cursors c WHERE c.target = m.target), 0)))
            """,
            (cutoff, keep_from, floor_all),
        ).fetchall()
        for r in rows:
            con.execute("DELETE FROM messages WHERE id = ?;", (r["id"],))
    for r in rows:
        try:
            p = Path(r["path"]).resolve()
            if p.parent == inbox and p.suffix == ".md":
                p.unlink()
        except OSError:
            pass
    return len(rows)
