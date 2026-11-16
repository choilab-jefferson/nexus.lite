"""nexus_lite.filebus - the file bus store (docs/specs/file-bus.md). Python stdlib only.

The bus directory is the source of truth: one JSON file per message, per-consumer cursor files, per-session
heartbeat files and a ``VERSION`` file. Correctness needs only: create a temp file, write, rename within one
directory, read, list, unlink. No lock, hard link, ``O_EXCL``, inotify or socket is required; the wake-up layer
(``Waiter`` / ``notify``) is a latency optimisation and a lost signal costs time, never a message.

    <bus>/VERSION  msg/<target>/<id>.json  blob/<target>/<id>.txt  cur/<target>/<consumer>.json
          sess/<name>.json  state/<name>.json

This module knows nothing of SQLite; ``memory.mtm`` keeps the rebuildable lite index on top of it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import select
import socket
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

from .protocol import validate_bus_field

FORMAT = "nexus.filebus"
VERSION = 1
MINOR = 0
SKEW_WINDOW_MS = 300_000          # W (spec 4.3, D1)
LIVE_TTL_S = 300.0                # session liveness (spec 7)
MAX_BODY = 256 * 1024             # inline body limit (spec D3)
INVALID_GRACE_S = 60.0            # a file invalid for this long is logged once (spec 5.3)
TMP_MAX_AGE_S = 3600.0            # orphan .tmp.* files older than this are pruned (spec 5.4)
DEFAULT_CONSUMER = "_default"
ALL = "ALL"
ID_RE = re.compile(r"^m[0-9]{13}-[0-9a-f]{8}-[0-9]{6}$")
_NAME_MAX = 128

log = logging.getLogger("nexus_lite.filebus")

PathLike = Union[str, "os.PathLike[str]"]


class BusError(RuntimeError):
    """A bus-level failure the CLI reports with exit 2."""


class BusLayoutError(BusError):
    """The bus directory has a layout this code does not support (spec 11)."""


# ------------------------------------------------------------------ names and ids
def check_name(kind: str, val: object) -> str:
    """`validate_bus_field` plus the file-name rules of spec 3: no leading '.' or '_', at most 128 bytes."""
    validate_bus_field(kind, val)
    assert isinstance(val, str)
    if val[0] in "._" or len(val.encode("utf-8")) > _NAME_MAX:
        raise ValueError(f"invalid {kind}: must not start with '.' or '_' and must be at most {_NAME_MAX} bytes")
    return val


def consumer_file(consumer: Optional[str]) -> str:
    """The cursor file stem of a consumer; the default consumer (None or "") is `_default`."""
    return check_name("consumer", consumer) if consumer else DEFAULT_CONSUMER


def id_ms(msg_id: str) -> int:
    return int(msg_id[1:14])


def floor_bound(cursor: Dict) -> str:
    """Lowest id string a consumer can still owe (ids with ms > hwm_ms - W); '' when nothing is acked.
    Usable as `fid >= bound` in an index query."""
    hwm = cursor.get("hwm") or ""
    if not hwm:
        return ""
    return f"m{max(id_ms(hwm) - int(cursor.get('w') or SKEW_WINDOW_MS) + 1, 0):013d}"


def is_unacked(cursor: Dict, msg_id: str) -> bool:
    """spec 6: id > hwm, or inside the skew window below hwm and not in `acked`."""
    hwm = cursor.get("hwm") or ""
    if not hwm or msg_id > hwm:
        return True
    if msg_id == hwm or msg_id in cursor.get("acked", ()):
        return False
    return id_ms(msg_id) > id_ms(hwm) - int(cursor.get("w") or SKEW_WINDOW_MS)


# ------------------------------------------------------------------ atomic file helpers
def _mkdirs(path: Path) -> None:
    todo: List[Path] = []
    p = path
    while not p.exists() and p != p.parent:
        todo.append(p)
        p = p.parent
    for d in reversed(todo):
        try:
            os.mkdir(str(d), 0o700)
        except FileExistsError:
            pass
        try:
            os.chmod(str(d), 0o700)
        except OSError:
            pass


def _tmp_name(final: Path) -> Path:
    return final.with_name(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")


def write_atomic(final: Path, data: bytes, *, fsync: bool = True) -> None:
    """tmp file in the directory of `final` (0600), write, best-effort fsync, rename. Readers see the old file or
    the complete new one. The temp is removed if the write or rename raises (a crash leaves an orphan `.tmp.*`)."""
    tmp = _tmp_name(final)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError as e:  # FUSE: logged, never fatal (spec 5.2)
                    log.debug("fsync failed on %s: %s", final.name, e)
        os.replace(str(tmp), str(final))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def _dump(obj: Dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path) -> Optional[Dict]:
    """The JSON object in `path`, or None (missing, unreadable, partial, not an object)."""
    try:
        with open(path, "rb") as f:
            obj = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _unlink(path: Union[Path, str]) -> bool:
    try:
        os.unlink(str(path))
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ per-process id writer
class _Writer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pid = -1
        self.wid = ""
        self.ctr = 0
        self.last_ms = 0

    def state(self) -> "_Writer":
        pid = os.getpid()
        if self.pid != pid:  # first use, or a forked child: it must not repeat the parent's (writer, ctr)
            self.pid, self.wid, self.ctr, self.last_ms = pid, secrets.token_hex(4), 0, 0
        return self

    def redraw(self) -> None:
        with self.lock:
            self.state().wid = secrets.token_hex(4)


_WRITER = _Writer()


# ------------------------------------------------------------------ the bus
_OPEN: Dict[str, "Bus"] = {}


def open_bus(root: PathLike) -> "Bus":
    """A (per-process cached) `Bus` for `root`; creates the layout and `VERSION` on first use."""
    p = Path(root).resolve()
    b = _OPEN.get(str(p))
    if b is not None and (p / "VERSION").exists():
        return b
    b = Bus(p)
    _OPEN[str(p)] = b
    return b


class Bus:
    def __init__(self, root: PathLike):
        self.root = Path(root)
        self.skew_ms = SKEW_WINDOW_MS
        self._newest: Dict[str, Tuple[float, int]] = {}
        self._bad: Dict[str, float] = {}
        self._bad_logged: set = set()
        self._open()

    # --- layout and version (spec 3, 11)
    def _open(self) -> None:
        vp = self.root / "VERSION"
        data = _read_json(vp)
        if data is None and vp.exists():
            for _ in range(5):  # a partial VERSION (A5) settles quickly
                time.sleep(0.02)
                data = _read_json(vp)
                if data is not None:
                    break
            if data is None:
                raise BusLayoutError(f"bus layout version unreadable: {vp}")
        if data is None:
            _mkdirs(self.root)
            data = {"format": FORMAT, "version": VERSION, "minor": MINOR, "skew_window_ms": SKEW_WINDOW_MS,
                    "created": time.time()}
            write_atomic(vp, _dump(data))
        ver = data.get("version")
        if not isinstance(ver, int) or isinstance(ver, bool) or ver > VERSION or data.get("format", FORMAT) != FORMAT:
            raise BusLayoutError(f"bus layout version {ver} unsupported (this nexus_lite reads version {VERSION})")
        try:
            self.skew_ms = int(data.get("skew_window_ms") or SKEW_WINDOW_MS)
        except (TypeError, ValueError):
            self.skew_ms = SKEW_WINDOW_MS
        for sub in ("msg", "cur", "sess"):
            _mkdirs(self.root / sub)

    def _mdir(self, target: str) -> Path:
        return self.root / "msg" / target

    # --- ids (spec 4)
    def new_id(self, target: str, now_ms: Optional[int] = None) -> str:
        w = _WRITER
        with w.lock:
            st = w.state()
            ms = max(now_ms if now_ms is not None else int(time.time() * 1000), st.last_ms)
            seen = self._newest.get(target)
            if seen is not None and time.monotonic() - seen[0] < 1.0:
                ms = max(ms, seen[1] + 1)  # writer clamp: never below what this writer just listed
            st.last_ms = ms
            st.ctr = (st.ctr + 1) % 1_000_000
            return f"m{ms:013d}-{st.wid}-{st.ctr:06d}"

    # --- messages (spec 5)
    def publish_message(self, target: str, msg_id: str, *, sender: str, kind: str, summary: str, body: str,
                        timestamp: Optional[float] = None, data: Optional[Dict] = None,
                        meta: Optional[Dict] = None) -> Dict:
        """Write one message under `msg/<target>/<msg_id>.json` (tmp + rename) and return the record. A body over
        256 KiB goes to `blob/<target>/<id>.txt` first and the message carries `body_ref`."""
        if not ID_RE.match(msg_id):
            raise ValueError(f"invalid message id {msg_id!r}")
        mdir = self._mdir(target)
        _mkdirs(mdir)
        rec: Dict = {
            "v": 1, "id": msg_id, "sender": sender, "target": target, "kind": kind, "summary": summary,
            "timestamp": time.time() if timestamp is None else timestamp,
            "body": body, "body_ref": None, "data": data,
            "meta": {"reply_to": None, "corr": None, "task_id": None, "sender_iid": None,
                     "sender_verified": None, "redis_id": None, "origin": "lite", **(meta or {})},
        }
        raw = body.encode("utf-8")
        if len(raw) > MAX_BODY:
            bdir = self.root / "blob" / target
            _mkdirs(bdir)
            write_atomic(bdir / f"{msg_id}.txt", raw)
            rec["body"], rec["body_ref"] = None, f"blob/{target}/{msg_id}.txt"
        write_atomic(mdir / f"{msg_id}.json", _dump(rec))
        return rec

    def send(self, target: str, *, sender: str, kind: str, summary: str, body: str, **kw) -> Dict:
        """Allocate an id and publish. A final name that already exists makes the writer draw a new writer id."""
        for _ in range(3):
            msg_id = self.new_id(target)
            if not os.path.lexists(self._mdir(target) / f"{msg_id}.json"):
                return self.publish_message(target, msg_id, sender=sender, kind=kind, summary=summary, body=body, **kw)
            _WRITER.redraw()
        raise BusError("could not allocate a unique message id")

    def targets(self) -> List[str]:
        try:
            return sorted(n for n in os.listdir(self.root / "msg") if n[:1] not in (".", "_")
                          and (self.root / "msg" / n).is_dir())
        except OSError:
            return []

    def list_ids(self, target: str) -> List[str]:
        """Message ids in `msg/<target>/` (name check only), ascending. Temp and foreign names are ignored."""
        try:
            names = os.listdir(self._mdir(target))
        except OSError:
            return []
        ids = sorted(n[:-5] for n in names if n.endswith(".json") and ID_RE.match(n[:-5]))
        # the writer clamp looks at OTHER writers' ids only: this writer's own last_ms already covers its ids, and
        # counting them would make a burst drift ahead of the wall clock by 1 ms per message
        mine = f"-{_WRITER.state().wid}-"
        newest = [id_ms(i) for i in reversed(ids) if mine not in i]
        if newest:
            self._newest[target] = (time.monotonic(), newest[0])
        return ids

    def read_message(self, target: str, msg_id: str) -> Optional[Dict]:
        """The valid record, or None (missing, partial, foreign, wrong target); invalid files are retried by the
        caller's next pass and logged once (id only) after 60 s (spec 5.3)."""
        path = self._mdir(target) / f"{msg_id}.json"
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            self._bad.pop(str(path), None)
            return None
        try:
            rec = json.loads(raw.decode("utf-8"))
        except ValueError:
            rec = None
        if self._valid(rec, msg_id, target):
            self._bad.pop(str(path), None)
            return rec
        first = self._bad.setdefault(str(path), time.monotonic())
        if time.monotonic() - first > INVALID_GRACE_S and str(path) not in self._bad_logged:
            self._bad_logged.add(str(path))
            log.warning("invalid bus message file %s/%s ignored", target, msg_id)
        return None

    @staticmethod
    def _valid(rec: object, stem: str, dirname: str) -> bool:
        if not isinstance(rec, dict):
            return False
        v = rec.get("v")
        if not isinstance(v, int) or isinstance(v, bool) or v > VERSION or v < 1:
            return False
        if rec.get("id") != stem:
            return False
        for k in ("sender", "target", "kind"):
            if not isinstance(rec.get(k), str) or not rec[k]:
                return False
        if rec["target"] not in (dirname, ALL):
            return False
        if not isinstance(rec.get("summary", ""), str):
            return False
        return isinstance(rec.get("body"), str) or isinstance(rec.get("body_ref"), str)

    def message_body(self, rec: Dict, target: str) -> str:
        """The full body of a record. When `body_ref` is set it wins (the blob is read; `body` is null or "" then);
        a missing blob falls back to an inline `body`, else reads as empty."""
        inline = rec["body"] if isinstance(rec.get("body"), str) else ""
        ref = rec.get("body_ref")
        if not isinstance(ref, str) or not ref:
            return inline
        if not ref.startswith(f"blob/{target}/") or ".." in ref:
            return inline
        try:
            with open(self.root / ref, "rb") as f:
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return inline

    def message_path(self, target: str, msg_id: str) -> Path:
        return self._mdir(target) / f"{msg_id}.json"

    def message_exists(self, target: str, msg_id: str) -> bool:
        return os.path.exists(self._mdir(target) / f"{msg_id}.json")

    # --- cursors (spec 6)
    def _cpath(self, target: str, consumer: Optional[str]) -> Path:
        return self.root / "cur" / target / f"{consumer_file(consumer)}.json"

    def read_cursor(self, target: str, consumer: Optional[str] = None) -> Dict:
        """{"hwm", "acked": set, "w", "exists"} for (target, consumer); an absent cursor acks nothing."""
        path = self._cpath(target, consumer)
        raw = None
        for attempt in range(3):
            raw = _read_json(path)
            if raw is not None or not path.exists():
                break
            time.sleep(0.01 * (attempt + 1))
        if raw is None:
            return {"hwm": "", "acked": set(), "w": self.skew_ms, "exists": False}
        hwm = raw.get("hwm") if isinstance(raw.get("hwm"), str) else ""
        acked = {a for a in raw.get("acked", []) if isinstance(a, str) and ID_RE.match(a)}
        return {"hwm": hwm if (not hwm or ID_RE.match(hwm)) else "", "acked": acked, "w": self.skew_ms,
                "exists": True}

    def _write_cursor(self, target: str, consumer: Optional[str], cur: Dict) -> None:
        d = self.root / "cur" / target
        _mkdirs(d)
        write_atomic(self._cpath(target, consumer), _dump({
            "v": 1, "target": target, "consumer": consumer_file(consumer), "hwm": cur["hwm"],
            "acked": sorted(cur["acked"]), "updated": time.time()}))

    def ack(self, target: str, consumer: Optional[str], ids: Iterable[str]) -> Dict:
        """Monotone, idempotent ack of `ids` (spec 6). Ids still unacked below the new hwm stay unacked."""
        ids = [i for i in ids if isinstance(i, str) and ID_RE.match(i)]
        cur = self.read_cursor(target, consumer)
        old_hwm, acked = cur["hwm"], set(cur["acked"])
        hwm = max([old_hwm] + ids)
        if old_hwm and hwm > old_hwm:
            acked.add(old_hwm)  # the old hwm is now inside the window below the new one
        acked.update(i for i in ids if i < hwm)
        if hwm:
            low = id_ms(hwm) - self.skew_ms
            acked = {a for a in acked if id_ms(a) > low}
        new = {"hwm": hwm, "acked": acked, "w": self.skew_ms, "exists": True}
        if not cur["exists"] or hwm != old_hwm or acked != cur["acked"]:
            self._write_cursor(target, consumer, new)
        return new

    def cursors(self, target: str) -> Dict[str, Dict]:
        """All consumer cursors of one target, by consumer file stem."""
        out: Dict[str, Dict] = {}
        try:
            names = os.listdir(self.root / "cur" / target)
        except OSError:
            return out
        for n in names:
            if n.endswith(".json") and not n.startswith("."):
                stem = n[:-5]
                out[stem] = self.read_cursor(target, None if stem == DEFAULT_CONSUMER else stem)
        return out

    def all_cursors(self) -> Dict[str, Dict[str, Dict]]:
        out: Dict[str, Dict[str, Dict]] = {}
        try:
            targets = [n for n in os.listdir(self.root / "cur") if n[:1] not in (".", "_")]
        except OSError:
            return out
        for t in targets:
            cs = self.cursors(t)
            if cs:
                out[t] = cs
        return out

    def unacked_ids(self, target: str, consumer: Optional[str] = None) -> List[str]:
        """Ids owed to (target, consumer) from the merged target + ALL inbox, oldest first."""
        cur = self.read_cursor(target, consumer)
        ids = set(self.list_ids(target))
        if target != ALL:
            ids.update(self.list_ids(ALL))
        return sorted(i for i in ids if is_unacked(cur, i))

    def audit(self, target: str, consumer: Optional[str] = None) -> List[str]:
        """Ids present in the inbox below the skew window of the cursor and not provably acked (spec 4.3/D1).
        Acked ids drop out of `acked` once outside the window, so acked-but-unpruned messages appear here too."""
        cur = self.read_cursor(target, consumer)
        hwm = cur["hwm"]
        if not hwm:
            return []
        ids = set(self.list_ids(target))
        if target != ALL:
            ids.update(self.list_ids(ALL))
        low = id_ms(hwm) - self.skew_ms
        return sorted(i for i in ids if i < hwm and id_ms(i) <= low and i not in cur["acked"])

    # --- sessions (spec 7)
    def _spath(self, name: str) -> Path:
        return self.root / "sess" / f"{check_name('name', name)}.json"

    def read_session(self, name: str) -> Optional[Dict]:
        path = self._spath(name)
        rec = _read_json(path)
        if rec is None:
            return None
        rec["live"] = self._is_live(rec, path)
        return rec

    @staticmethod
    def _is_live(rec: Dict, path: Path, now: Optional[float] = None) -> bool:
        if rec.get("status") != "live":
            return False
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0.0
        try:
            updated = float(rec.get("updated") or 0.0)
            ttl = float(rec.get("ttl_s") or LIVE_TTL_S)
        except (TypeError, ValueError):
            return False
        return (time.time() if now is None else now) - max(updated, mtime) <= ttl

    def sessions(self) -> List[Dict]:
        out = []
        try:
            names = os.listdir(self.root / "sess")
        except OSError:
            return out
        for n in sorted(names):
            if n.endswith(".json") and not n.startswith("."):
                rec = _read_json(self.root / "sess" / n)
                if rec is not None and isinstance(rec.get("name"), str):
                    rec["live"] = self._is_live(rec, self.root / "sess" / n)
                    out.append(rec)
        return out

    def register(self, name: str, *, notify: Optional[Dict] = None, agent: Optional[Dict] = None,
                 pid: Optional[int] = None, ttl_s: float = LIVE_TTL_S) -> Tuple[Dict, bool]:
        """Mark `name` live (register or heartbeat). Returns (record, was_live)."""
        prev = self.read_session(name)
        was_live = bool(prev and prev["live"])
        now = time.time()
        rec = {"v": 1, "name": name, "status": "live", "pid": pid, "host": socket.gethostname(),
               "kernel": self._kernel(name), "started": (prev or {}).get("started") if was_live else now,
               "updated": now, "ttl_s": int(ttl_s),
               "notify": notify if notify is not None else (prev or {}).get("notify"),
               "agent": agent if agent is not None else (prev or {}).get("agent")}
        rec["started"] = rec["started"] or now
        write_atomic(self._spath(name), _dump(rec), fsync=False)
        return rec, was_live

    def unregister(self, name: str) -> bool:
        """status -> unregistered (the file stays). True if the session had a file."""
        prev = _read_json(self._spath(name))
        if prev is None:
            return False
        prev.update(status="unregistered", updated=time.time())
        write_atomic(self._spath(name), _dump(prev), fsync=False)
        return True

    def import_session(self, name: str, status: str, started: Optional[float], updated: Optional[float]) -> None:
        """Write a session file from legacy registry values (migration only)."""
        write_atomic(self._spath(name), _dump({
            "v": 1, "name": name, "status": status, "pid": None, "host": socket.gethostname(),
            "kernel": self._kernel(name), "started": started or 0.0, "updated": updated or 0.0,
            "ttl_s": int(LIVE_TTL_S), "notify": None, "agent": None}), fsync=False)
        try:  # a migrated heartbeat is as old as the registry row said, whatever the file's mtime
            os.utime(self._spath(name), (updated or 0.0, updated or 0.0))
        except OSError:
            pass

    def set_session_live(self, name: str, live: bool) -> bool:
        """Update-only (never creates): flip an existing session file live/unregistered."""
        prev = _read_json(self._spath(name))
        if prev is None:
            return False
        prev.update(status="live" if live else "unregistered", updated=time.time())
        write_atomic(self._spath(name), _dump(prev), fsync=False)
        return True

    def _kernel(self, name: str) -> str:
        try:
            with open("/proc/sys/kernel/random/boot_id") as f:
                return f.read().strip()
        except OSError:
            pass
        sp = self.root / "state" / f"{name}.json"
        st = _read_json(sp)
        if st and isinstance(st.get("kernel"), str):
            return st["kernel"]
        k = secrets.token_hex(8)
        try:
            _mkdirs(sp.parent)
            write_atomic(sp, _dump({**(st or {}), "kernel": k}), fsync=False)
        except OSError:
            pass
        return k

    # --- prune (spec 8.1)
    def prune(self, max_age_s: float, keep_last: int = 0, *, now: Optional[float] = None) -> List[Tuple[str, str]]:
        """Unlink acked, old messages (and their blobs); returns [(target, id)]. Also unlinks `.tmp.*` older than
        1 h and invalid message files older than `max_age_s`. Safe to run from several hosts (ENOENT ignored)."""
        now = time.time() if now is None else now
        cutoff_ms = (now - max_age_s) * 1000.0
        cursors = self.all_cursors()
        flat = [c for cs in cursors.values() for c in cs.values()]
        live = [s["name"] for s in self.sessions() if s["live"]]
        pin_all = (not flat) or any(n not in cursors for n in live)
        deleted: List[Tuple[str, str]] = []
        below_window = 0
        for target in self.targets():
            ids = self.list_ids(target)
            keep = set(ids[-keep_last:]) if keep_last > 0 else set()
            curs = (flat if not pin_all else []) if target == ALL else list(cursors.get(target, {}).values())
            for i in ids:
                if i in keep or id_ms(i) >= cutoff_ms or not curs:
                    continue
                if all(c["hwm"] >= i and not is_unacked(c, i) for c in curs):
                    if any(i not in c["acked"] and i != c["hwm"] and id_ms(i) <= id_ms(c["hwm"]) - self.skew_ms
                           for c in curs):
                        below_window += 1
                    if _unlink(self._mdir(target) / f"{i}.json"):
                        deleted.append((target, i))
                    _unlink(self.root / "blob" / target / f"{i}.txt")
            self._prune_dir_junk(self._mdir(target), now, max_age_s, validate=target)
        for sub in ("blob", "cur", "sess"):
            for d in self._subdirs(sub):
                self._prune_dir_junk(d, now, max_age_s, validate=None)
        if below_window:
            log.info("prune: %d message(s) below the skew window were not provably acked", below_window)
        return deleted

    def _subdirs(self, sub: str) -> List[Path]:
        base = self.root / sub
        try:
            names = os.listdir(base)
        except OSError:
            return []
        out = [base] if sub == "sess" else []
        out.extend(base / n for n in names if (base / n).is_dir())
        return out

    def _prune_dir_junk(self, d: Path, now: float, max_age_s: float, validate: Optional[str]) -> None:
        try:
            names = os.listdir(d)
        except OSError:
            return
        for n in names:
            if validate is not None and not n.startswith(".") and (
                    not ID_RE.match(n[:-5]) or id_ms(n[:-5]) >= (now - max_age_s) * 1000.0):
                continue  # only old message files can be pruned as invalid
            p = d / n
            try:
                age = now - os.stat(p).st_mtime
            except OSError:
                continue
            if n.startswith(".tmp.") and age > TMP_MAX_AGE_S:
                _unlink(p)
            elif (validate is not None and n.endswith(".json") and ID_RE.match(n[:-5]) and age > max_age_s):
                try:
                    rec = json.loads(p.read_bytes().decode("utf-8"))
                except OSError:
                    continue
                except ValueError:
                    rec = None
                if not self._valid(rec, n[:-5], validate):
                    _unlink(p)

    def prune_sessions(self, max_age_s: float, *, now: Optional[float] = None) -> int:
        """Remove `unregistered` session files older than `max_age_s` and their cursors."""
        now = time.time() if now is None else now
        n = 0
        for s in self.sessions():
            if s.get("status") == "unregistered" and now - float(s.get("updated") or 0.0) > max_age_s:
                if _unlink(self._spath(s["name"])):
                    n += 1
                for stem in list(self.cursors(s["name"])):
                    _unlink(self.root / "cur" / s["name"] / f"{stem}.json")
        return n

    # --- wake-up (spec 10)
    def notify(self, target: str, msg_id: str) -> None:
        """Best-effort push after a message is renamed into place: local datagram sockets of the target's waiters
        (every waiter for ALL), the Unix socket / loopback TCP port in the target's session file. Never raises."""
        try:
            _notify(self, target, msg_id)
        except Exception:  # a lost signal costs latency only
            pass


# ------------------------------------------------------------------ wake-up layer
def run_dir() -> Path:
    """Local runtime dir for waiter sockets (never inside a bus dir); the rule is normative (spec implementation
    note 4) so every implementation computes the same path:
    base = $NEXUS_BUS_RUN_DIR, else $XDG_RUNTIME_DIR/nexus-bus, else ($TMPDIR or /tmp)/nexus-bus-<uid>;
    a base longer than 60 UTF-8 bytes (AF_UNIX paths are limited to ~104) is replaced by /tmp/nexus-bus-<uid>."""
    env = os.environ.get("NEXUS_BUS_RUN_DIR")
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    tmp = os.environ.get("TMPDIR") or "/tmp"
    base = Path(env) if env else (Path(xdg) / "nexus-bus" if xdg else Path(tmp) / f"nexus-bus-{uid}")
    if len(str(base).encode()) > 60:
        base = Path("/tmp") / f"nexus-bus-{uid}"
    return base


def _boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _bus_run_dir(root: Path) -> Path:
    return run_dir() / _key(str(Path(root).resolve()))


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _dgram(path: str, payload: bytes) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.setblocking(False)
        s.sendto(payload, path)
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        _unlink(path)  # a dead waiter's socket
    except OSError:
        pass
    finally:
        s.close()
    return False


def _tcp_ping(hostport: str, payload: bytes) -> None:
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit() or not _is_loopback(host.strip("[]")):  # loopback only (D4)
        return
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=0.2) as c:
            c.sendall(payload + b"\n")
    except OSError:
        pass


def _notify(bus: Bus, target: str, msg_id: str) -> None:
    payload = msg_id.encode("ascii")[:64]
    d = _bus_run_dir(bus.root)
    try:
        entries = os.listdir(d)
    except OSError:
        entries = []
    for e in entries:  # every socket in the per-bus dir, whatever its target: the payload is only a "rescan" hint
        if e.endswith(".sock"):
            _dgram(str(d / e), payload)
    names = [s["name"] for s in bus.sessions()] if target == ALL else [target]
    me = socket.gethostname()
    local_kernel = _boot_id()
    for n in names:
        rec = bus.read_session(n) if n and n[:1] not in "._" else None
        note = (rec or {}).get("notify") or {}
        if not isinstance(note, dict):
            continue
        if note.get("unix") and rec.get("host") == me and (local_kernel is None or rec.get("kernel") == local_kernel):
            _dgram(str(note["unix"]), payload)
        if note.get("tcp"):
            _tcp_ping(str(note["tcp"]), payload)


class Waiter:
    """A bound wake-up endpoint for one target: a Unix datagram socket in `run_dir()` (and, with `tcp=True`, a
    loopback TCP listener). Create it BEFORE the first scan so a message that lands in between still wakes the
    next `wait`. If binding fails the waiter degrades to a plain sleep (the poll fallback)."""

    def __init__(self, root: PathLike, target: str, *, tcp: bool = False):
        self._sock: Optional[socket.socket] = None
        self._lsock: Optional[socket.socket] = None
        self.path: Optional[str] = None
        self.tcp_addr: Optional[str] = None
        try:
            d = _bus_run_dir(Path(root))
            _mkdirs(d)
            path = str(d / f"{_key(target)}.{os.getpid()}.{secrets.token_hex(2)}.sock")
            if len(path.encode()) < 100:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    s.bind(path)
                except OSError:
                    s.close()
                    raise
                s.setblocking(False)
                self._sock, self.path = s, path
        except OSError:
            pass
        if tcp:
            try:
                ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ls.bind(("127.0.0.1", 0))  # loopback only (D4)
                ls.listen(8)
                ls.setblocking(False)
                self._lsock = ls
                self.tcp_addr = f"127.0.0.1:{ls.getsockname()[1]}"
            except OSError:
                pass

    def wait(self, timeout: float) -> bool:
        """Sleep up to `timeout` s; True if a wake-up arrived (the payload is only a hint: always rescan)."""
        socks = [s for s in (self._sock, self._lsock) if s is not None]
        if not socks:
            time.sleep(max(timeout, 0.0))
            return False
        try:
            ready, _, _ = select.select(socks, [], [], max(timeout, 0.0))
        except (OSError, ValueError):
            time.sleep(max(timeout, 0.0))
            return False
        for s in ready:
            self._drain(s)
        return bool(ready)

    def _drain(self, s: socket.socket) -> None:
        try:
            if s is self._lsock:
                while True:
                    c, _ = s.accept()
                    c.close()
            while True:
                s.recv(256)
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        for s in (self._sock, self._lsock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        if self.path:
            _unlink(self.path)
        self._sock = self._lsock = None
        self.path = None

    def __enter__(self) -> "Waiter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
