"""
nexus_lite.pty.control — session control API: spawn, send, keys, read, wait, kill and discovery (stdlib only).

Features:
- Sessions served by ``python -m nexus_lite.pty serve`` (no dtach binary needed; the binary is used only with
  NEXUS_LITE_DTACH=binary)
- dtach 10-byte wire protocol (see :mod:`nexus_lite.pty.protocol`), spoken through :class:`~.client.Connection`
- Screen rendering with :mod:`nexus_lite.pty.screen` (no third-party terminal emulator)
- Per-socket flock concurrency control (<sock>.lock)
- Multi-agent state classifier (claude, agy, gemini, copilot, opencode, bash)
- Session lifecycle: spawn, send, keys, read, wait, list_sessions, kill
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, Union

from .._db import get_workspace, mark_session
from .._proc import reap_in_background
from .agents import normalize_agent_cmd, provider_of
from .client import Connection
from .protocol import (  # noqa: F401  (re-exported by the package)
    MSG_ATTACH, MSG_DETACH, MSG_PUSH, MSG_REDRAW, MSG_WINCH, PKT, PUSH_MAX, packet, probe_connect, sock_lock,
    winch_packet,
)
from .screen import Screen, render  # noqa: F401

_PKT = PKT
_PUSH_MAX = PUSH_MAX
_packet = packet
_winch_packet = winch_packet
_probe_connect = probe_connect

# Common spinners used across AI agent CLIs
SPINNERS = (
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
    "⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷",
)

# Capture/render geometry (must match the winch sent in spawn() and the capture worker).
CAPTURE_ROWS = 45
CAPTURE_COLS = 240
# read() only feeds the tail of the scrollback to pyte; the capture worker rotates the file.
SCROLLBACK_TAIL_BYTES = 1 << 20
SCROLLBACK_MAX_BYTES = 4 << 20
# Seconds the screen must stay unchanged before an idle verdict after send() (non-prompt providers).
DEFAULT_SETTLE = 2.0
# Providers whose idle verdict is a visible prompt; the rest are "not busy" and need a settle window.
_PROMPT_PROVIDERS = ("claude", "bash")

CLAUDE_BUSY_RE = re.compile(r"^[ \t]*[·✢✳✶✻✽][ \t]+[^\s…]+…", re.MULTILINE)
BASH_PROMPT_RE = re.compile(r"[$#%>❯]\s*$", re.MULTILINE)

KEY_NAMES: dict[str, bytes] = {
    "enter": b"\r",
    "return": b"\r",
    "cr": b"\r",
    "lf": b"\n",
    "tab": b"\t",
    "btab": b"\x1b[Z",
    "esc": b"\x1b",
    "escape": b"\x1b",
    "space": b" ",
    "bs": b"\x7f",
    "bspace": b"\x7f",
    "backspace": b"\x7f",
    "del": b"\x1b[3~",
    "delete": b"\x1b[3~",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pgup": b"\x1b[5~",
    "pageup": b"\x1b[5~",
    "pgdn": b"\x1b[6~",
    "pagedown": b"\x1b[6~",
    "c-c": b"\x03",
    "ctrl-c": b"\x03",
    "c-d": b"\x04",
    "ctrl-d": b"\x04",
    "c-z": b"\x1a",
    "ctrl-z": b"\x1a",
    "c-l": b"\x0c",
    "ctrl-l": b"\x0c",
    "c-u": b"\x15",
    "ctrl-u": b"\x15",
    "c-a": b"\x01",
    "ctrl-a": b"\x01",
    "c-e": b"\x05",
    "ctrl-e": b"\x05",
    "c-r": b"\x12",
    "ctrl-r": b"\x12",
    "c-w": b"\x17",
    "ctrl-w": b"\x17",
}

KEY_MAP: dict[str, bytes] = dict(KEY_NAMES)


def key_to_bytes(tok: str) -> bytes:
    """Convert a key token (KEY_NAMES/KEY_MAP, c-/ctrl-<char>, m-/alt-<char>, or literal) to bytes."""
    if not tok:
        return b""
    low = tok.lower()
    if low in KEY_NAMES:
        return KEY_NAMES[low]
    if low in KEY_MAP:
        return KEY_MAP[low]
    stripped = low.strip()
    if stripped in KEY_NAMES:
        return KEY_NAMES[stripped]
    if stripped in KEY_MAP:
        return KEY_MAP[stripped]
    for prefix in ("c-", "ctrl-"):
        if stripped.startswith(prefix) and len(stripped) == len(prefix) + 1:
            ch = stripped[-1]
            if "a" <= ch <= "z":
                return bytes([ord(ch) - ord("a") + 1])
            specials = {"[": 27, "\\": 28, "]": 29, "^": 30, "_": 31, " ": 0, "@": 0}
            if ch in specials:
                return bytes([specials[ch]])
    for prefix in ("m-", "alt-"):
        if stripped.startswith(prefix) and len(stripped) == len(prefix) + 1:
            orig_ch = tok.strip()[-1]
            return b"\x1b" + orig_ch.encode("utf-8")
    return tok.encode("utf-8")


def keys_to_bytes(tokens: Iterable[str]) -> bytes:
    """Convert an iterable of key tokens (or a whitespace-separated string) to bytes."""
    if isinstance(tokens, str):
        tokens = tokens.split()
    return b"".join(key_to_bytes(t) for t in tokens)


SUN_PATH_MAX = 103   # macOS sun_path is 104 bytes with the NUL (Linux 108)
SOCK_NAME_BUDGET = 40  # session-name bytes the default directory must leave room for
_DRIVE_FUSE_PREFIXES = ("/content/drive", "/mnt/drive", "/Volumes/GoogleDrive")
_SOCK_DIR_CAPABLE: dict[str, bool] = {}


def _on_drive_fuse(path: Union[str, Path]) -> bool:
    """Return True if path resides on a Drive FUSE mount where AF_UNIX bind() is unsupported."""
    raw = str(path)
    candidates = [raw]
    with contextlib.suppress(OSError):
        resolved = str(Path(path).resolve())
        if resolved != raw:
            candidates.append(resolved)
    for cand in candidates:
        for prefix in _DRIVE_FUSE_PREFIXES:
            if cand == prefix or cand.startswith(prefix + "/"):
                return True
    return False


def _dir_supports_unix_socket(d: str) -> bool:
    """Probe whether directory `d` (or its closest existing parent) can bind an AF_UNIX socket."""
    if d in _SOCK_DIR_CAPABLE:
        return _SOCK_DIR_CAPABLE[d]
    probe_dir = d
    while probe_dir and not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    if not probe_dir or not os.path.isdir(probe_dir):
        return False
    probe_sock = os.path.join(probe_dir, f".nx-p-{os.getpid()}-{threading.get_ident() % 100000}")
    if len(probe_sock.encode()) > SUN_PATH_MAX:
        probe_sock = os.path.join(probe_dir, f".nxp-{os.getpid()}")
    ok = False
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(probe_sock)
        sock.bind(probe_sock)
        ok = True
    except OSError:
        ok = False
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        with contextlib.suppress(OSError):
            os.unlink(probe_sock)
    _SOCK_DIR_CAPABLE[d] = ok
    return ok


def default_sock_dir() -> str:
    """Workspace-scoped runtime socket dir: <ws>/run when local, short, and AF_UNIX-capable;
    or /tmp/nx-<workspace hash> otherwise. Never a plain shared /tmp; the hash only separates namespaces."""
    ws = get_workspace()
    run = str(ws / "run")
    if (
        not _on_drive_fuse(ws)
        and len(run.encode()) + len("/nexus-lite-.sock") + SOCK_NAME_BUDGET <= SUN_PATH_MAX
        and _dir_supports_unix_socket(run)
    ):
        return run
    return "/tmp/nx-" + hashlib.sha256(str(ws).encode()).hexdigest()[:12]


def _ensure_private_dir(d: str) -> None:
    """Create `d` 0700; refuse a symlink or a directory owned by another user (a pre-planted /tmp path)."""
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f"socket directory {d} is not a directory owned by this user; refusing to use it")


def get_sock_dir() -> str:
    """Socket directory: NEXUS_LITE_SOCK_DIR if set, else the workspace-scoped default_sock_dir().
    spawn, lookup, pointer injection and kill all resolve sockets through this one helper."""
    d = os.environ.get("NEXUS_LITE_SOCK_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    d = default_sock_dir()
    if d.startswith("/tmp/nx-"):
        _ensure_private_dir(d)
    else:
        os.makedirs(d, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(d, 0o700)
    return d


def sock_path(name: str) -> str:
    """Resolve a session name or socket reference to an absolute socket path."""
    if "/" in name or name.endswith(".sock"):
        return os.path.abspath(name)
    sock_dir = get_sock_dir()
    clean = name[:-5] if name.endswith(".sock") else name
    if clean.startswith("nexus-lite-"):
        clean = clean[len("nexus-lite-"):]
    path = os.path.join(sock_dir, f"nexus-lite-{clean}.sock")
    if len(path.encode()) > 107:
        raise ValueError(
            f"socket path too long ({len(path.encode())} > 107 bytes): {path}; "
            "set NEXUS_LITE_SOCK_DIR to a shorter directory"
        )
    return path


def session_name(sock_p: str) -> str:
    """Extract clean session name from socket path."""
    base = os.path.basename(sock_p)
    if base.endswith(".sock"):
        base = base[:-5]
    if base.startswith("nexus-lite-"):
        base = base[len("nexus-lite-"):]
    return base



def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: exists
    return True


def _read_pid_sidecar(sock_path: str) -> Optional[int]:
    try:
        pid = int(Path(f"{sock_path}.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    if pid > 0 and _pid_alive(pid) and _pid_argv_has(pid, sock_path):
        return pid
    return None


def pid_argv(pid: int) -> Optional[list]:
    """Full argv of ``pid``, or None. Linux: ``/proc/<pid>/cmdline`` split on NUL (exact, never truncated).
    Elsewhere: ``ps -ww`` (unlimited width; without it ps may cut the command at the column width)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
        return shlex.split(out) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _pid_argv_has(pid: int, token: str) -> bool:
    """True if ``token`` is an exact argv element of ``pid`` (PID-reuse guard)."""
    return token in (pid_argv(pid) or [])


def _write_pid_sidecar(sock_path: str, pid: int) -> None:
    try:
        Path(f"{sock_path}.pid").write_text(f"{pid}\n")
    except OSError:
        pass


def _ps_table() -> list:
    """(pid, ppid, pgid) of every process, from ``ps`` (Linux and macOS)."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=5).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(x.isdigit() for x in parts):
            rows.append(tuple(int(x) for x in parts))
    return rows


def _leader_pgid(master_pid: int) -> Optional[int]:
    """pgid of the PTY leader (the master's child that leads its own group). None if not exactly one."""
    try:
        groups = {pgid for pid, ppid, pgid in _ps_table() if ppid == master_pid and pid == pgid}
    except Exception:
        return None
    return groups.pop() if len(groups) == 1 else None


def _record_pgid(sock_path: str) -> None:
    """Write <sock>.pgid for the session's PTY leader group (best effort; kill falls back to the master)."""
    master = find_dtach_master_pid(sock_path)
    end = time.monotonic() + 2.0
    pgid = None
    while master and pgid is None and time.monotonic() < end:
        pgid = _leader_pgid(master)
        if pgid is None:
            time.sleep(0.05)
    if pgid:
        with contextlib.suppress(OSError):
            Path(f"{sock_path}.pgid").write_text(f"{pgid}\n")


def _group_members(pgid: int) -> list:
    try:
        return [pid for pid, _ppid, g in _ps_table() if g == pgid]
    except Exception:
        return []


def _kill_group(pgid: int, grace_s: float = 1.0) -> list:
    """SIGTERM the group, wait up to grace_s, SIGKILL the rest; return pids still in the group afterwards.
    Our own group is never signalled."""
    if pgid <= 1 or pgid == os.getpgid(0):
        return []
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    end = time.monotonic() + grace_s
    while time.monotonic() < end and _group_members(pgid):
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    end = time.monotonic() + 1.0
    while time.monotonic() < end and _group_members(pgid):
        time.sleep(0.05)
    return _group_members(pgid)


PTYD_MODULE = "nexus_lite.ptyd"  # pre-package module name; python -m nexus_lite.ptyd is still a valid launcher
PTY_MODULE = "nexus_lite.pty"


def _is_master_argv(argv: list, sock_path: str) -> bool:
    """argv of a process that may own ``sock_path``: the stdlib server (``python -m nexus_lite.pty serve ... SOCK -- CMD``,
    or the pre-package ``python -m nexus_lite.ptyd ... SOCK -- CMD``),
    a dtach binary (``dtach -n|-A|-c SOCK ...``, sessions started with NEXUS_LITE_DTACH=binary or before the switch),
    or the pre-ptyd stdlib daemon (``python -m nexus_lite.pty --serve SOCK -- CMD``). Attach clients carry the
    socket too; callers tell masters apart by their child (the PTY program)."""
    if not argv or sock_path not in argv:
        return False
    if os.path.basename(argv[0]) == "dtach":
        return True
    try:
        cut = argv.index("--")
    except ValueError:
        cut = len(argv)
    head = argv[:cut]
    if sock_path not in head:
        return False
    return PTYD_MODULE in head or (PTY_MODULE in head and ("serve" in head or "--serve" in head))


def _scan_proc(sock_path: str) -> Optional[int]:
    """Linux /proc scan; see find_dtach_master_pid."""
    matches: list[int] = []
    try:
        for cmdline_path in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                with open(cmdline_path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            argv = [a.decode("utf-8", "replace") for a in raw.split(b"\x00") if a]
            if _is_master_argv(argv, sock_path):
                try:
                    matches.append(int(cmdline_path.split("/")[2]))
                except (IndexError, ValueError):
                    continue
    except Exception:
        return None

    with_children: list[int] = []
    for pid in matches:
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as f:
                kids = f.read().split()
        except OSError:
            continue
        if kids:
            with_children.append(pid)
    return with_children[0] if len(with_children) == 1 else None


def _scan_ps(sock_path: str) -> Optional[int]:
    """macOS/BSD fallback: parse ``ps -ww -axo pid=,ppid=,command=`` (no pgrep needed)."""
    try:
        out = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,ppid=,command="],  # -ww: never truncate the command column
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return None
    matches: list[int] = []
    parents: set[int] = set()
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            argv = shlex.split(parts[2])
        except ValueError:
            continue
        parents.add(ppid)
        if _is_master_argv(argv, sock_path):
            matches.append(pid)
    with_children = [p for p in matches if p in parents]
    return with_children[0] if len(with_children) == 1 else None


def find_dtach_master_pid(sock_path: str) -> Optional[int]:
    """PID of the process that OWNS ``sock_path`` (its master, not a client).

    Order: (1) live ``<sock>.pid`` sidecar (written by spawn() and the stdlib
    server itself); (2) Linux ``/proc`` scan; (3) ``ps`` parse when ``/proc``
    is absent (macOS/BSD). The scans pick server processes (``nexus_lite.pty serve``,
    or a ``dtach`` binary / the old ``nexus_lite.pty --serve`` daemon, see
    ``_is_master_argv``) whose argv carries ``sock_path`` as an EXACT token; only the
    master owns a child (the inner PTY leader), so exactly one candidate with
    children wins; zero or several is ambiguous -> None. Never guesses. Never
    raises."""
    try:
        pid = _read_pid_sidecar(sock_path)
        if pid is not None:
            return pid
        if os.path.isdir("/proc"):
            return _scan_proc(sock_path)
        return _scan_ps(sock_path)
    except Exception:
        return None


_find_dtach_pid = find_dtach_master_pid  # backward-compatible alias


def _start_capture_worker(sp: str) -> None:
    """Start background process to stream socket output into <sock>.scrollback."""
    code = f"""
import socket, struct, sys, os
sock = {sp!r}
sb = sock + '.scrollback'
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock)
    _PKT = struct.Struct('<BB8s')
    s.sendall(_PKT.pack(5, 0, b"\\x00"*8))  # MSG_SIDECAR: an observer, not a person (ignored by dtach masters)
    s.sendall(_PKT.pack(1, 0, b"\\x00"*8))
    ws = struct.pack('<HHHH', {CAPTURE_ROWS}, {CAPTURE_COLS}, 0, 0)
    s.sendall(_PKT.pack(3, 0, ws))
    f = open(sb, 'ab')
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        f.write(chunk)
        f.flush()
        if f.tell() > {SCROLLBACK_MAX_BYTES}:
            f.close()
            with open(sb, 'rb') as r:
                r.seek(-{SCROLLBACK_TAIL_BYTES}, 2)
                tail = r.read()
            with open(sb, 'wb') as w:
                w.write(tail)
            f = open(sb, 'ab')
except Exception:
    pass
"""
    try:
        reap_in_background(subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ))
    except Exception:
        pass


def spawn(
    cmd: Union[str, list[str]],
    name: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> str:
    """Launch a command inside a persistent PTY session and return session name.

    The session is served by ``python -m nexus_lite.pty serve`` (stdlib only); NEXUS_LITE_DTACH=binary uses an
    installed dtach binary instead. Both speak the same wire protocol."""
    cmd = normalize_agent_cmd(cmd, mode="pty")

    if not name:
        name = f"sess-{os.urandom(4).hex()}"

    sp = sock_path(name)
    if os.path.exists(sp):
        if _probe_connect(sp):
            raise RuntimeError(f"Session '{name}' is already running at {sp}")
        else:
            try:
                os.unlink(sp)
            except OSError:
                pass
            for sibling in (f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark", f"{sp}.pid", f"{sp}.pgid", f"{sp}.meta", f"{sp}.input"):
                if os.path.exists(sibling):
                    try:
                        os.unlink(sibling)
                    except OSError:
                        pass

    cmd_str = " ".join(shlex.quote(a) for a in cmd) if isinstance(cmd, list) else str(cmd)
    shell = os.environ.get("SHELL", "/bin/bash")
    cd_prefix = f"cd {shlex.quote(str(cwd))} && " if cwd else ""
    inner = f"{cd_prefix}{cmd_str}"
    argv = _master_argv(sp, [shell, "-lc", inner])
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              env=env, timeout=30, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Failed to start PTY session {name!r}: {e}") from e
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"Failed to start PTY session {name!r} (rc={proc.returncode}): {err}")

    # The launcher returns after binding the socket; wait until it accepts connections.
    start = time.monotonic()
    sock_ready = False
    while time.monotonic() - start < 4.0:
        if os.path.exists(sp) and _probe_connect(sp):
            sock_ready = True
            break
        time.sleep(0.02)

    if not sock_ready:
        raise RuntimeError(f"PTY session socket {sp} did not come up within 4 seconds")

    if os.path.basename(argv[0]) == "dtach":  # the stdlib server writes its own sidecar
        mp = _scan_proc(sp) if os.path.isdir("/proc") else _scan_ps(sp)
        if mp:
            _write_pid_sidecar(sp, mp)
    _record_pgid(sp)

    # Set initial terminal geometry (240 cols x 45 rows)
    try:
        with Connection(sp, timeout=1.0) as conn:
            conn.attach()
            conn.winch(CAPTURE_ROWS, CAPTURE_COLS)
            conn.detach()
    except Exception:
        pass

    # Start scrollback capture and await readiness
    _start_capture_worker(sp)
    sb_path = f"{sp}.scrollback"
    t_wait = time.monotonic()
    while time.monotonic() - t_wait < 1.5:
        if os.path.exists(sb_path):
            time.sleep(0.05)
            break
        time.sleep(0.02)

    mark_session(name, True)  # a (re)spawned name is live on the bus registry
    with contextlib.suppress(OSError):  # provider label and creation time for `pty ls`
        Path(f"{sp}.meta").write_text(json.dumps({"provider": provider_of(cmd), "created": time.time()}))
    return name


def send(name: str, text: Union[str, bytes], enter: bool = True) -> None:
    """Inject keystrokes or string into the session under sock_lock."""
    sp = sock_path(name)
    if not os.path.exists(sp) or not _probe_connect(sp):
        raise RuntimeError(f"Session '{name}' is not running ({sp})")

    data = text.encode("utf-8") if isinstance(text, str) else text
    # Fingerprint BEFORE pushing: a fast command can finish before a post-push mark,
    # which would hide its output from wait() ("no activity since send").
    fp_before = _fingerprint(sp)

    with sock_lock(sp):
        with Connection(sp) as conn:
            conn.push(data)  # 8-byte PUSH records
            if enter:
                time.sleep(0.05)
                conn.push(b"\r")
    _write_sendmark(name, sp, fp_before)


def _fingerprint(sp: str) -> str:
    """Cheap fingerprint of the captured output: size + hash of the tail."""
    sb = f"{sp}.scrollback"
    try:
        size = os.path.getsize(sb)
        with open(sb, "rb") as f:
            f.seek(max(0, size - 4096))
            tail = f.read()
    except OSError:
        return "0:"
    return f"{size}:{hashlib.sha1(tail).hexdigest()}"


def _write_sendmark(name: str, sp: str, fp: Optional[str] = None) -> None:
    """Record the output fingerprint at send time so wait() can require activity after it."""
    try:
        with open(f"{sp}.sendmark", "w") as f:
            json.dump({"fp": fp if fp is not None else _fingerprint(sp), "t": time.time()}, f)
    except OSError:
        pass


def _read_sendmark(sp: str) -> Optional[str]:
    try:
        with open(f"{sp}.sendmark") as f:
            return json.load(f).get("fp")
    except (OSError, ValueError):
        return None


def keys(name: str, *tokens: str) -> None:
    """Send specialized key tokens (e.g. 'Enter', 'c-c', 'Tab', 'Esc') to session."""
    sp = sock_path(name)
    if not os.path.exists(sp) or not _probe_connect(sp):
        raise RuntimeError(f"Session '{name}' is not running ({sp})")

    with sock_lock(sp):
        with Connection(sp) as conn:
            for token in tokens:
                conn.push(key_to_bytes(token))
                time.sleep(0.02)


def read(name: str, cols: int = CAPTURE_COLS, rows: int = CAPTURE_ROWS, settle: float = 0.25) -> str:
    """Read the session screen: the scrollback tail rendered by :mod:`nexus_lite.pty.screen` (trimmed text)."""

    sp = sock_path(name)
    if settle > 0:
        time.sleep(settle)

    sb_path = f"{sp}.scrollback"
    raw = b""
    if os.path.exists(sb_path):
        try:
            with open(sb_path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size > SCROLLBACK_TAIL_BYTES:
                    f.seek(size - SCROLLBACK_TAIL_BYTES)
                raw = f.read()
        except OSError:
            pass

    # If scrollback is not present or empty, fallback to direct attach & winch
    if not raw and os.path.exists(sp) and _probe_connect(sp):
        try:
            with Connection(sp, timeout=2.0) as conn:
                conn.attach()
                # Nudge the size so a redraw happens even when it equals the current geometry.
                conn.winch(rows + 1, cols)
                time.sleep(0.1)
                conn.winch(rows, cols)
                raw = conn.collect(0.3)
                with contextlib.suppress(OSError):
                    conn.detach()
        except Exception:
            pass

    if not raw:
        return ""

    return render(raw, cols, rows)


_SPIN = list(SPINNERS)

# Per-provider screen markers (pure text; lite's historic markers kept). Keys:
#   needs_input: literals for a blocked dialog   busy/busy_re: working markers
#   idle: literals marking the input prompt      idle_last_line: last line looks
#   like a shell prompt                          idle_default: not-busy => idle
# The "" entry is the fallback for unknown providers.
PROVIDER_MANIFESTS: dict[str, dict] = {
    "claude": {
        "busy": ["esc to interrupt"] + _SPIN, "busy_re": [CLAUDE_BUSY_RE],
        "idle": ["❯", "> "],
    },
    "agy": {
        "needs_input": ["Do you trust the contents"],
        "busy": ["Analyzing…", "Generating", "esc to cancel"] + _SPIN,
        "idle": ["accept-edits", "❯", "> "], "idle_default": True,
    },
    "gemini": {
        "needs_input": ["Do you trust the files in this folder?"],
        "busy": ["Thinking…", "esc to cancel"] + _SPIN,
        "idle": ["Type your message", "YOLO", "> "],
    },
    "copilot": {
        "needs_input": ["Do you trust the files in this folder?",
                        "Do you want to approve this request?"],
        "busy": ["Working", "esc cancel"] + _SPIN,
        "idle": ["Do you want to", "> ", "❯"], "idle_default": True,
    },
    "opencode": {
        "needs_input": ["Permission required", "Access external directory"],
        "busy": ["Thinking…"] + _SPIN,
        "idle": ["Permission required", "> "], "idle_default": True,
    },
    "codex": {
        "needs_input": ["Would you like to run the following command?"],
        "busy": ["Working", "esc to interrupt"] + _SPIN,
        "idle": ["⏎ send", "send"],
    },
    "bash": {"idle_last_line": True},
    "": {
        "busy": ["Thinking…", "Analyzing…", "esc to interrupt", "esc to cancel"] + _SPIN,
        "busy_re": [CLAUDE_BUSY_RE], "idle": ["❯", "> "],
        "idle_last_line": True, "idle_default": True,
    },
}


def _manifest(provider: str) -> dict:
    return PROVIDER_MANIFESTS.get((provider or "").lower().strip(), PROVIDER_MANIFESTS[""])


def _has_busy(m: dict, text: str) -> bool:
    return any(x in text for x in m.get("busy", ())) or any(
        r.search(text) for r in m.get("busy_re", ()))


def _has_idle_marker(m: dict, text: str) -> bool:
    if any(x in text for x in m.get("idle", ())):
        return True
    if m.get("idle_last_line"):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines and (BASH_PROMPT_RE.search(lines[-1])
                      or lines[-1].endswith(("$", "#", "%", ">", "❯"))):
            return True
    return False


def classify(text: str, provider: str = "") -> str:
    """Classify rendered screen ``text`` into a ``pty_state``
    vocabulary: ``"none"`` (blank), ``"needs_input"`` (blocked dialog),
    ``"working"``, or ``"idle"``. Precedence needs_input > working > idle; a
    non-blank screen matching nothing degrades to ``"idle"``, never to
    ``"needs_input"``. Unknown providers use the generic ``""`` manifest."""
    if not text or not text.strip():
        return "none"
    m = _manifest(provider)
    if any(x in text for x in m.get("needs_input", ())):
        return "needs_input"
    if _has_busy(m, text):
        return "working"
    return "idle"


def is_busy(text: str, provider: str = "") -> bool:
    """True when the screen shows the agent or process actively working.

    Busy markers win even over a needs_input dialog (a spinner next to a trust
    prompt is still busy) — unlike :func:`classify`, which reports needs_input."""
    return bool(text) and _has_busy(_manifest(provider), text)


def is_idle(text: str, provider: str = "") -> bool:
    """True when the screen shows the agent waiting for input and not busy."""
    if not text or not text.strip() or is_busy(text, provider):
        return False
    m = _manifest(provider)
    return _has_idle_marker(m, text) or bool(m.get("idle_default"))


def wait(
    name: str,
    provider: str = "",
    timeout: float = 30.0,
    poll: float = 0.5,
    settle: float = DEFAULT_SETTLE,
    auto_trust: Optional[bool] = None,
) -> str:
    """Wait until the session is idle and return the rendered screen.

    After a send(), idle requires activity since the send (output changed), no busy
    marker, and (for providers without a visible ready prompt) an unchanged screen for
    `settle` seconds. Raises TimeoutError otherwise. auto_trust (default: env
    NEXUS_LITE_PTY_AUTO_TRUST=1, else off) answers a trust prompt with 'y'.
    """
    if auto_trust is None:
        auto_trust = os.environ.get("NEXUS_LITE_PTY_AUTO_TRUST", "") == "1"
    prov = provider.lower().strip()
    sp = sock_path(name)
    mark = _read_sendmark(sp)
    deadline = time.monotonic() + timeout
    prev_screen = None
    stable_since = 0.0
    stable_count = 0
    last_screen = ""

    while time.monotonic() < deadline:
        last_screen = read(name, settle=0.0)
        if last_screen:
            if auto_trust and "Do you trust the contents" in last_screen:
                keys(name, "y", "Enter")
                time.sleep(0.5)
                prev_screen = None
                stable_count = 0
                continue
            busy = is_busy(last_screen, provider)
            if mark is not None:
                changed = _fingerprint(sp) != mark
                if last_screen != prev_screen:
                    prev_screen = last_screen
                    stable_since = time.monotonic()
                if changed and not busy:
                    if prov in _PROMPT_PROVIDERS:
                        if is_idle(last_screen, provider):
                            return last_screen
                    elif time.monotonic() - stable_since >= settle:
                        return last_screen
            else:
                if is_idle(last_screen, provider) and not busy:
                    return last_screen
                # Stability fallback: screen unchanged and not busy
                if last_screen == prev_screen and not busy:
                    stable_count += 1
                    if stable_count >= 2:
                        return last_screen
                else:
                    stable_count = 0
                    prev_screen = last_screen
        time.sleep(poll)

    raise TimeoutError(f"Session '{name}' did not become idle within {timeout}s")


def list_sessions() -> list[dict]:
    """List all live nexus-lite PTY sessions."""
    sock_dir = get_sock_dir()
    pattern = os.path.join(sock_dir, "nexus-lite-*.sock")
    socks = sorted(glob.glob(pattern))
    sessions: list[dict] = []

    for sp in socks:
        name = session_name(sp)
        alive = _probe_connect(sp)
        if not alive:
            # Check if PID is alive
            pid = find_dtach_master_pid(sp)
            if pid is None:
                # Cleanup stale socket and lock
                try:
                    os.unlink(sp)
                except OSError:
                    pass
                for sibling in (f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark", f"{sp}.pid", f"{sp}.pgid", f"{sp}.meta", f"{sp}.input"):
                    if os.path.exists(sibling):
                        try:
                            os.unlink(sibling)
                        except OSError:
                            pass
                continue
        sessions.append({"name": name, "sock": sp, "alive": True})
    return sessions


def _read_pgid(sock_path: str) -> Optional[int]:
    try:
        return int(Path(f"{sock_path}.pgid").read_text().strip())
    except (OSError, ValueError):
        return None


def kill_session(name: str) -> list:
    """Terminate a session: its dtach master and the PTY leader's whole process group (recorded at spawn in
    <sock>.pgid, so it works even when the master is already gone). Cleans up socket artifacts and returns the pids
    of that group that still survive (normally [])."""
    sp = sock_path(name)
    pid = find_dtach_master_pid(sp)
    pgid = _read_pgid(sp) or (_leader_pgid(pid) if pid else None)
    if pid is not None:
        try:
            os.kill(pid, 15)  # SIGTERM
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.05)
            if _pid_alive(pid):
                os.kill(pid, 9)   # SIGKILL
        except OSError:
            pass
    leftovers = _kill_group(pgid) if pgid else []

    for path in (sp, f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark", f"{sp}.pid", f"{sp}.pgid", f"{sp}.meta", f"{sp}.input"):
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    mark_session(name, False)  # killed -> not live on the bus registry
    return leftovers


def kill(name: str) -> bool:
    """Terminate a session and its process group (see kill_session) and clean up socket artifacts."""
    kill_session(name)
    return True


def inject_or_send(
    name: str,
    text: Optional[str] = None,
    *,
    bus_kind: Optional[str] = None,
    summary: Optional[str] = None,
    enter: bool = True,
    settle_s: float = 1.0,
    tail_lines: int = 40,
) -> str:
    """Drive a live session from outside and return its settled screen tail.

    ``bus_kind`` unset: push raw ``text`` via MSG_PUSH (``send``). ``bus_kind``
    set: deliver ``text`` (or ``summary``) as a structured bus message to
    ``name`` via ``mtm.bus_send`` (the session sees the ``[nexus] <kind> ...``
    pointer line). Then wait ``settle_s`` and return the last ``tail_lines``
    lines of the pyte-rendered screen (``read``)."""
    if bus_kind:
        from .memory import mtm  # lazy: mtm imports this module
        body = "\n".join(x for x in (summary, text) if x)
        mtm.bus_send(name, body, kind=bus_kind)
    elif text is not None:
        send(name, text, enter=enter)
    else:
        raise ValueError("inject_or_send needs text or bus_kind")
    screen = read(name, settle=settle_s)
    return "\n".join(screen.splitlines()[-tail_lines:])


# ---------------------------------------------------------------------------
# Session server selection and interactive attach
# ---------------------------------------------------------------------------

def _use_dtach_binary() -> bool:
    """True only on explicit opt-in (NEXUS_LITE_DTACH=binary); the default is the stdlib server."""
    return os.environ.get("NEXUS_LITE_DTACH", "").lower() in ("binary", "dtach")


def _master_argv(sp: str, cmd: list) -> list:
    """Launcher argv for a detached session on ``sp`` running ``cmd`` (redraw method: winch).
    Both forms carry ``sp`` as an exact argv token of the long-lived master."""
    if _use_dtach_binary():
        dtach = shutil.which("dtach")
        if dtach is None:
            raise RuntimeError("NEXUS_LITE_DTACH=binary but no dtach binary on PATH")
        return [dtach, "-n", sp, "-z", "-r", "winch", *cmd]
    return [sys.executable, "-m", PTY_MODULE, "serve", "-r", "winch", sp, "--", *cmd]


def attach(name: str, detach_char: Optional[bytes] = b"\x1c", *, ro: bool = False, label: Optional[str] = None,
           redraw: Optional[int] = None) -> int:
    """Attach the calling terminal to session ``name`` interactively (raw mode, every key goes to the program,
    no suspend key). ``detach_char`` (default Ctrl-\\, None disables) detaches; the session keeps running. ``ro``:
    watch only, never sends PUSH (``q`` also detaches). ``label``: print ``[attached: LABEL ...]`` /
    ``[detached: LABEL]``. ``redraw``: redraw method sent on attach (protocol REDRAW_* value; default: the session's)."""
    from . import client as _client
    from .protocol import REDRAW_UNSPEC
    sp = sock_path(name)
    if not os.path.exists(sp):
        raise RuntimeError(f"Session '{name}' is not running ({sp})")
    return _client.attach(sp, detach_char=detach_char, redraw=REDRAW_UNSPEC if redraw is None else redraw, ro=ro,
                          label=label)


def read_meta(sp: str) -> dict:
    """``<sock>.meta`` written by :func:`spawn` (``provider``, ``created``); ``{}`` for sessions not started by it."""
    try:
        meta = json.loads(Path(f"{sp}.meta").read_text())
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def last_human_input(sock: str) -> Optional[float]:
    """Epoch time of the last keystroke an interactive client (one that sent ATTACH: ``attach``, ``dtach -a``, a
    terminal) typed into the session, from ``<sock>.input`` written by the stdlib server; None when nobody has typed
    or the session is not served by it (a ``dtach`` binary keeps no stamp). Programmatic ``send`` / ``keys``
    connections never attach, so they do not count. ``sock`` is a socket path or a session name."""
    sp = sock if ("/" in sock or sock.endswith(".sock")) else sock_path(sock)
    try:
        return float(Path(f"{sp}.input").read_text().strip())
    except (OSError, ValueError):
        return None
