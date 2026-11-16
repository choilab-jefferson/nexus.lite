"""
nexus_lite.memory.mtm — MTM: file bus with a local SQLite index and inbox pointer delivery.

MTM = flow carried across interruptions; its transport is this bus (nexus wording; MTM+ is retired).
`bus_send` is the lite verb for what nexus calls say/dm/relay; target `ALL` = broadcast to live sessions.

The bus directory (`<workspace>/bus`, or `$NEXUS_BUS_DIR`) is the source of truth (`..filebus`, spec
docs/specs/file-bus.md): one JSON file per message, cursor files, session files. `lite.db` (`messages`,
`bus_cursors`, `sessions`) is lite's local, rebuildable index of it: `seq` is an index-local ingest counter,
`bus_reindex` rebuilds the rows from the files. Per D2 every message still gets a legacy body file
`<ws>/inbox/<id>.md`, and `path` points at it. Edges: `.._db` (workspace/SQLite), `..filebus` (the store),
`..protocol` (pointer-line format), `..pty` (live session delivery).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Iterable, List, Optional, Union

from .. import filebus, pty
from ..artifacts import mkdir_private, replace_private
from .._db import connect, get_bus_dir, get_workspace, init
from ..protocol import format_pointer_line, validate_bus_field


# A registered session counts as live while its heartbeat is newer than this (seconds).
LIVE_TTL_S = filebus.LIVE_TTL_S

_COLS = "id, sender, target, kind, summary, path, content, timestamp, seq, data"


def _out(r) -> dict:
    """An index row as the API dict: `data` (typed-kind payload) decoded from its JSON text, `fid` dropped."""
    d = {k: r[k] for k in r.keys() if k != "fid"}
    raw = d.get("data")
    try:
        d["data"] = json.loads(raw) if raw else None
    except ValueError:
        d["data"] = None
    return d

# Under NEXUS_SENSITIVITY=phi a `task`/`handoff` message may only carry a pointer-shaped
# one-liner (`TASK_COMPLETE <id>: <status>`); anything else could be agent output and is redacted.
_PHI_KINDS = ("task", "handoff")
_PHI_SAFE_RE = re.compile(r"^(TASK_COMPLETE|TASK_ESCALATE|HANDOFF)[\w .:\-]{0,200}$")
PHI_REDACTED = "[phi-redacted]"
_LEGACY_ID_RE = re.compile(r"^m-[0-9a-f]{8}$")


def _phi_safe_message(kind: str, message: str) -> str:
    if os.environ.get("NEXUS_SENSITIVITY") == "phi" and kind in _PHI_KINDS:
        if not _PHI_SAFE_RE.match(message.strip()):
            return PHI_REDACTED
    return message


def _norm_target(target: str) -> str:
    """`ALL` is case-insensitive in the API and stored upper-case; every target obeys the file-name rules."""
    validate_bus_field("target", target)
    t = filebus.ALL if target.upper() == "ALL" else target
    return filebus.check_name("target", t)


def _bus() -> filebus.Bus:
    return filebus.open_bus(get_bus_dir())


def _live_names(bus: filebus.Bus) -> set[str]:
    """Bus-live sessions. A dtach session whose socket probes alive is live regardless of its heartbeat (no
    heartbeat needed), unless its session file says status != 'live' (bus_unregister / pty.kill). A live socket
    with NO session file is live too, so sessions created before the registry existed (upgrade) still receive
    ALL. Session files without a socket are live only while the heartbeat is within the TTL."""
    sess = {s["name"]: s for s in bus.sessions()}
    live = {n for n, s in sess.items() if s["live"]}
    for s in pty.list_sessions():
        if sess.get(s["name"], {"status": "live"}).get("status") == "live":
            live.add(s["name"])
    return live


def _inject(name: str, pointer: str) -> bool:
    try:
        pty.send(name, pointer, enter=True)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ the local index
def _begin(con) -> None:
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE;")  # serialise seq allocation across processes


def _data_text(rec: dict) -> Optional[str]:
    d = rec.get("data")
    return json.dumps(d, ensure_ascii=False) if d is not None else None


def _next_seq(con) -> int:
    seq = con.execute("SELECT value FROM bus_meta WHERE key = 'last_seq';").fetchone()[0] + 1
    con.execute("UPDATE bus_meta SET value = ? WHERE key = 'last_seq';", (seq,))
    return seq


def _body_path(ws: Path, bus: filebus.Bus, target: str, rec: dict) -> str:
    """`path` of an indexed message: its legacy body file when one exists (D2), else the message JSON."""
    for stem in (rec["id"], (rec.get("meta") or {}).get("legacy_id")):
        if isinstance(stem, str) and stem and "/" not in stem:
            p = ws / "inbox" / f"{stem}.md"
            if p.is_file():
                return str(p)
    return str(bus.message_path(target, rec["id"]))


def _ingest(con, bus: filebus.Bus, targets: Iterable[str]) -> None:
    """Make the index agree with the files for `targets`: new valid message files get the next `seq` (in id order),
    rows whose file vanished are deleted. Cheap: one listdir and one index query per target."""
    ws = get_workspace()
    for t in dict.fromkeys(targets):
        ids = bus.list_ids(t)
        idset = set(ids)
        have = {r[0] for r in con.execute("SELECT fid FROM messages WHERE fid IS NOT NULL AND target = ?;", (t,))}
        gone = [i for i in have - idset if not bus.message_exists(t, i)]
        parsed = []
        for i in ids:
            if i not in have:
                rec = bus.read_message(t, i)
                if rec is not None:
                    parsed.append(rec)
        if not parsed and not gone:
            continue
        _begin(con)
        if parsed:  # recheck under the write lock: another process may have ingested the same files
            have = {r[0] for r in con.execute("SELECT fid FROM messages WHERE fid IS NOT NULL AND target = ?;", (t,))}
            for rec in parsed:
                if rec["id"] in have:
                    continue
                con.execute(
                    "INSERT INTO messages (id, sender, target, kind, summary, path, content, timestamp, seq, fid, data) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (rec["id"], rec["sender"], t, rec["kind"], rec.get("summary") or "", _body_path(ws, bus, t, rec),
                     bus.message_body(rec, t), rec.get("timestamp"), _next_seq(con), rec["id"], _data_text(rec)),
                )
        for i in gone:
            con.execute("DELETE FROM messages WHERE fid = ? AND target = ?;", (i, t))


def _inbox_targets(target: str) -> List[str]:
    return [target] if target == filebus.ALL else [target, filebus.ALL]


def _cursor_row(con, bus: filebus.Bus, target: str, consumer: Optional[str], cur: dict, floor: int = 0) -> int:
    """Refresh the index cursor row of (target, consumer) from its cursor file: acked_seq = the highest index seq
    whose id is acked, never lower than before (a cache; the file decides what is delivered)."""
    derived = 0
    rows = con.execute(
        "SELECT fid, seq FROM messages WHERE fid IS NOT NULL AND (target = ? OR target = 'ALL') AND fid >= ?;",
        (target, filebus.floor_bound(cur)),
    ).fetchall()
    for r in rows:
        if not filebus.is_unacked(cur, r["fid"]):
            derived = max(derived, r["seq"])
    row = con.execute(
        "SELECT acked_seq FROM bus_cursors WHERE target = ? AND consumer = ?;", (target, consumer or "")
    ).fetchone()
    new = max(row["acked_seq"] if row else 0, derived, floor)
    con.execute(
        "INSERT INTO bus_cursors (target, consumer, acked_seq, updated) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(target, consumer) DO UPDATE SET acked_seq = excluded.acked_seq, updated = excluded.updated;",
        (target, consumer or "", new, time.time()),
    )
    return new


# ------------------------------------------------------------------ send / read / ack
def bus_send(
    target: str,
    message: str,
    sender: str = "user",
    kind: str = "dm",
    *,
    data: Optional[dict] = None,
    meta: Optional[dict] = None,
    summary: Optional[str] = None,
) -> dict:
    """
    Send a message via the message bus.
    Writes the message file `<bus>/msg/<target>/<id>.json` (atomic rename), the legacy body file
    `.nexus/inbox/<id>.md`, indexes it in the SQLite `messages` table (integer `seq`, local to this index), wakes
    local waiters, and injects '[nexus] <kind> <id> <path> - <summary>' into the target's PTY.
    ALL fans out to every bus-live session (see _live_names): live dtach sockets + fresh registrations.
    `data` is the structured payload of typed kinds (e.g. `job`); `summary` overrides the first-line summary.
    """
    target = _norm_target(target)
    validate_bus_field("sender", sender)
    validate_bus_field("kind", kind)
    if not isinstance(message, str):
        raise TypeError(f"message must be str, got {type(message).__name__}")
    message = _phi_safe_message(kind, message)
    ws = init()
    bus = _bus()
    mkdir_private(ws / "inbox")

    if summary is None:
        lines = [ln.strip() for ln in message.strip().splitlines() if ln.strip()]
        summary = lines[0][:100] if lines else ""
    ts = time.time()
    msg_id = bus.new_id(target)
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
    try:
        bus.publish_message(target, msg_id, sender=sender, kind=kind, summary=summary, body=message,
                            timestamp=ts, data=data, meta=meta)
    except BaseException:
        path.unlink(missing_ok=True)
        raise

    with connect() as con:
        _ingest(con, bus, [target])
        row = con.execute("SELECT seq FROM messages WHERE fid = ? AND target = ?;", (msg_id, target)).fetchone()
        seq = row["seq"] if row else None
    bus.notify(target, msg_id)
    live = _live_names(bus)

    pointer = format_pointer_line(kind, msg_id, str(path), summary)

    live_sessions = pty.list_sessions()
    if target == filebus.ALL:
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
        "data": data,
    }


def bus_read(
    target: Optional[str] = None,
    limit: int = 20,
    since_seq: Optional[int] = None,
    unacked: bool = False,
    consumer: Optional[str] = None,
    *,
    since_id: Optional[str] = None,
    audit: bool = False,
) -> list[dict]:
    """Read messages, optionally filtered by target.

    Default: newest first (unchanged). With `since_seq` / `since_id` and/or `unacked` (needs `target`): oldest
    first; `unacked` = the file-cursor unacked set of `consumer` (None = the default cursor), in index (`seq`) order,
    `since_seq` filters on the index `seq` and `since_id` (exclusive) is the file-native equivalent.
    `audit` (needs `target`) lists inbox messages below the consumer's skew window that are not provably acked.
    """
    init()
    if target is not None:
        target = _norm_target(target)
    if (unacked or audit) and not target:
        raise ValueError("unacked and audit require target")
    if since_id is not None and not isinstance(since_id, str):
        raise TypeError(f"since_id must be str, got {type(since_id).__name__}")
    bus = _bus()
    with connect() as con:
        _ingest(con, bus, _inbox_targets(target) if target else bus.targets())
        if audit:
            ids = bus.audit(target, consumer)
            if not ids:
                return []
            marks = ",".join("?" for _ in ids)
            cur = con.execute(
                f"SELECT {_COLS} FROM messages WHERE fid IN ({marks}) ORDER BY fid LIMIT ?", (*ids, limit))
            return [_out(r) for r in cur.fetchall()]
        if since_seq is None and since_id is None and not unacked:
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
            return [_out(r) for r in cur.fetchall()]
        if unacked:
            cursor = bus.read_cursor(target, consumer)
            rows = con.execute(
                f"SELECT {_COLS}, fid FROM messages WHERE fid IS NOT NULL AND (target = ? OR target = 'ALL') "
                "AND fid >= ?;",
                (target, filebus.floor_bound(cursor)),
            ).fetchall()
            out = [
                r for r in rows
                if filebus.is_unacked(cursor, r["fid"])
                and (since_seq is None or r["seq"] > since_seq)
                and (since_id is None or r["fid"] > since_id)
            ]
            out.sort(key=lambda r: r["seq"])  # index (delivery) order, so a seq ack never skips an earlier row
            return [_out(r) for r in out[:limit]]
        where, args = ["seq > ?"], [int(since_seq or 0)]
        if since_id is not None:
            where.append("fid > ?")
            args.append(since_id)
        if target:
            where.append("(target = ? OR target = 'ALL')")
            args.append(target)
        cur = con.execute(
            f"SELECT {_COLS} FROM messages WHERE {' AND '.join(where)} ORDER BY seq LIMIT ?", (*args, limit)
        )
        return [_out(r) for r in cur.fetchall()]


def _floor_ids(con, target: str, seq: int, cursor: dict) -> list[str]:
    """File ids of the index rows (target + ALL) with seq <= `seq` that can still matter to the cursor."""
    rows = con.execute(
        "SELECT fid FROM messages WHERE fid IS NOT NULL AND (target = ? OR target = 'ALL') AND seq <= ? AND fid >= ?;",
        (target, seq, filebus.floor_bound(cursor)),
    ).fetchall()
    return [r["fid"] for r in rows]


def bus_ack(target: str, seq: Union[int, str], consumer: Optional[str] = None) -> int:
    """Ack `target`'s messages for `consumer` (None = default): every indexed message with seq <= `seq`, or just the
    message id when `seq` is a file id. Idempotent and monotone; a cursor never moves back. Returns the index's
    acked_seq (a cache, never lower than `seq` as before)."""
    target = _norm_target(target)
    if consumer:
        filebus.check_name("consumer", consumer)
    by_id = isinstance(seq, str) and (filebus.ID_RE.match(seq) or _LEGACY_ID_RE.match(seq))
    if not by_id and (not isinstance(seq, int) or isinstance(seq, bool)):
        raise TypeError(f"seq must be int or a message id, got {type(seq).__name__}")
    init()
    bus = _bus()
    with connect() as con:
        _ingest(con, bus, _inbox_targets(target))
        if by_id:
            row = con.execute("SELECT fid FROM messages WHERE id = ? OR fid = ?;", (seq, seq)).fetchone()
            ids = [row["fid"]] if row and row["fid"] else []
            floor = 0
        else:
            ids = _floor_ids(con, target, seq, bus.read_cursor(target, consumer))
            floor = seq
        _begin(con)
        cur = bus.ack(target, consumer, ids)
        return _cursor_row(con, bus, target, consumer, cur, floor)


# ------------------------------------------------------------------ wait / watch
def _idle_interval(poll_s: float, idle_s: float) -> float:
    """Adaptive backoff for long idle waits (the wake-up layer is the fast path; this only paces the rescan)."""
    return poll_s if idle_s < 60.0 else max(poll_s, min(5.0, poll_s * 4))


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
    """Deliver new messages for `target` oldest-first via an independent cursor, woken by the wake-up layer
    (local notify) with a `poll_s` rescan as the fallback.

    Generator by default (ends after `timeout` seconds, or when `stop.is_set()`); with
    `callback` it runs the loop itself, calls `callback(msg)` per message and returns the
    count. `consumer` (default "watch") keeps its own cursor, so default/"turn" are untouched.
    `ack=True` advances the cursor after the message is handled (at-least-once); `ack=False`
    leaves it, so a later watch re-yields.
    """
    if callback is None:
        return _watch_iter(target, consumer, poll_s, timeout, ack, stop)
    n = 0
    for m in _watch_iter(target, consumer, poll_s, timeout, ack, stop):
        callback(m)
        n += 1
    return n


def bus_wait(
    target: str,
    *,
    timeout: Optional[float] = None,
    poll_s: float = 1.0,
    since_seq: Optional[int] = None,
    consumer: Optional[str] = None,
    limit: int = 100,
    since_id: Optional[str] = None,
) -> list[dict]:
    """Block until `target` has an unacked message (seq > since_seq / id > since_id if given), then return them
    oldest-first. Blocks on the wake-up layer (a datagram socket the sender pings after its rename) and rescans
    every `poll_s` as the fallback, so a lost signal costs latency only.

    Nothing is acked: the caller handles the messages and acks them (`bus_ack`), so a message that arrived while
    nobody was waiting returns at once on the next wait. Returns [] on timeout. For push-style watching: an agent
    runs `nexus-lite bus wait` as a background job and is woken when it exits.
    """
    target = _norm_target(target)
    init()
    deadline = None if timeout is None else time.monotonic() + timeout
    t0 = time.monotonic()
    with filebus.Waiter(get_bus_dir(), target) as w:  # bound before the first scan: no lost wake-up
        while True:
            msgs = bus_read(target=target, unacked=True, since_seq=since_seq, consumer=consumer, limit=limit,
                            since_id=since_id)
            if msgs:
                return msgs
            step = _idle_interval(poll_s, time.monotonic() - t0)
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return []
                step = min(step, left)
            w.wait(step)


def _watch_iter(target, consumer, poll_s, timeout, ack, stop):
    deadline = None if timeout is None else time.monotonic() + timeout
    target = _norm_target(target)
    init()
    # Without ack the cursor never moves; track it locally so one watch yields each message once.
    seen = 0
    t0 = time.monotonic()
    w = filebus.Waiter(get_bus_dir(), target)  # bound before the first scan
    try:
        while True:
            if stop is not None and stop.is_set():
                return
            msgs = bus_read(target=target, unacked=True, consumer=consumer, limit=100, since_seq=seen)
            for m in msgs:
                seen = max(seen, m["seq"])
                yield m
                if ack:
                    bus_ack(target, m["seq"], consumer=consumer)
            if msgs:
                t0 = time.monotonic()
                continue  # drain backlog before sleeping
            step = _idle_interval(poll_s, time.monotonic() - t0)
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                step = min(step, left)
            w.wait(step)
    finally:
        w.close()


# ------------------------------------------------------------------ sessions
def _mirror_session(name: str, status: str, now: float) -> None:
    """The `sessions` table is a cache of the session files; keep the row in step (best effort)."""
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO sessions (name, created_at, status, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET status = excluded.status, last_seen = excluded.last_seen;",
                (name, now, status, now),
            )
    except Exception:
        pass


def bus_register(name: str, notify: Optional[dict] = None) -> dict:
    """Mark session `name` live (writes/refreshes `sess/<name>.json`). On a not-live -> live transition, re-emit
    the pointer for each direct message unread by any consumer cursor in use (lowest cursor wins; no
    cursor = everything). Already-live re-register = heartbeat only. `notify` is an optional
    `{"unix": path, "tcp": "127.0.0.1:port"}` wake-up address advertised in the session file."""
    filebus.check_name("name", name)
    init()
    bus = _bus()
    _rec, was_live = bus.register(name, notify=notify)
    _mirror_session(name, "live", time.time())
    pending: list[dict] = []
    if not was_live:
        with connect() as con:
            _ingest(con, bus, [name])
            cursors = list(bus.cursors(name).values())
            rows = con.execute(
                f"SELECT {_COLS}, fid FROM messages WHERE fid IS NOT NULL AND target = ? ORDER BY fid;", (name,)
            ).fetchall()
            pending = [r for r in rows if not cursors or any(filebus.is_unacked(c, r["fid"]) for c in cursors)]
    for m in pending:
        _inject(name, format_pointer_line(m["kind"], m["id"], m["path"], m["summary"]))
    return {"name": name, "redelivered": [m["id"] for m in pending]}


def bus_unregister(name: str) -> bool:
    """Mark session `name` not live (the session file stays). Returns True if it was registered."""
    filebus.check_name("name", name)
    init()
    ok = _bus().unregister(name)
    if ok:
        _mirror_session(name, "unregistered", time.time())
    return ok


# ------------------------------------------------------------------ prune / reindex
def bus_prune(max_age_s: float, keep_last: int = 0, *, sessions: bool = False, now: Optional[float] = None) -> int:
    """Delete messages older than `max_age_s` that are acked (direct: every consumer cursor of the target has
    acked it; ALL: every cursor acked it and every live registered session has a cursor), never the newest
    `keep_last` of a directory. Also `.tmp.*` orphans older than 1 h and invalid files older than `max_age_s`;
    unlinks the message file, its blob and its legacy inbox body. With `sessions`, drops unregistered sessions
    older than `max_age_s` and their cursors. Returns the number of messages deleted. Safe to run on several
    hosts at once."""
    if not isinstance(max_age_s, (int, float)) or not isinstance(keep_last, int):
        raise TypeError("max_age_s must be a number and keep_last an int")
    init()
    bus = _bus()
    inbox = (get_workspace() / "inbox").resolve()
    with connect() as con:
        _ingest(con, bus, bus.targets())
    deleted = bus.prune(max_age_s, keep_last, now=now)
    cutoff = (time.time() if now is None else now) - max_age_s
    paths: list[str] = []
    with connect() as con:
        _begin(con)
        for t, i in deleted:
            row = con.execute("SELECT path FROM messages WHERE fid = ? AND target = ?;", (i, t)).fetchone()
            if row:
                paths.append(row["path"])
                con.execute("DELETE FROM messages WHERE fid = ? AND target = ?;", (i, t))
        # legacy rows (acked by every cursor when they were migrated) age out of the index with their bodies
        legacy = con.execute(
            "SELECT id, path FROM messages WHERE fid IS NULL AND timestamp < ?;", (cutoff,)).fetchall()
        for r in legacy:
            paths.append(r["path"])
            con.execute("DELETE FROM messages WHERE id = ?;", (r["id"],))
        _ingest(con, bus, bus.targets())  # rows of files another host pruned meanwhile
    for raw in paths:
        try:
            p = Path(raw).resolve()
            if p.parent == inbox and p.suffix == ".md":
                p.unlink()
        except (OSError, TypeError):
            pass
    if sessions:
        bus.prune_sessions(max_age_s, now=now)
    return len(deleted) + len(legacy)


def bus_reindex() -> dict:
    """Rebuild the local index from the bus dir: drop every file-backed message row, cursor row and session row
    (legacy rows without a file stay) and re-ingest all message files in id order. Files win every discrepancy.
    Returns {"messages": n, "cursors": n, "sessions": n}."""
    init()
    bus = _bus()
    entries = []
    for t in bus.targets():
        for i in bus.list_ids(t):
            rec = bus.read_message(t, i)
            if rec is not None:
                entries.append((i, t, rec))
    entries.sort(key=lambda e: e[0])
    ws = get_workspace()
    cursors = 0
    with connect() as con:
        _begin(con)
        con.execute("DELETE FROM messages WHERE fid IS NOT NULL;")
        con.execute("DELETE FROM bus_cursors;")
        top = con.execute("SELECT MAX(seq) FROM messages;").fetchone()[0] or 0
        con.execute("UPDATE bus_meta SET value = ? WHERE key = 'last_seq';", (top,))
        for i, t, rec in entries:
            con.execute(
                "INSERT INTO messages (id, sender, target, kind, summary, path, content, timestamp, seq, fid, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (i, rec["sender"], t, rec["kind"], rec.get("summary") or "", _body_path(ws, bus, t, rec),
                 bus.message_body(rec, t), rec.get("timestamp"), _next_seq(con), i, _data_text(rec)),
            )
        for t, per in bus.all_cursors().items():
            for stem, cur in per.items():
                _cursor_row(con, bus, t, None if stem == filebus.DEFAULT_CONSUMER else stem, cur)
                cursors += 1
        sess = bus.sessions()
        for s in sess:
            con.execute(
                "INSERT INTO sessions (name, created_at, status, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET status = excluded.status, last_seen = excluded.last_seen;",
                (s["name"], s.get("started"), s.get("status"), s.get("updated")),
            )
    return {"messages": len(entries), "cursors": cursors, "sessions": len(sess)}
