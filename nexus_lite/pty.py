"""
nexus_lite.pty — Persistent PTY controller over dtach wire protocol and pyte.

Features:
- dtach 10-byte wire protocol (<BB8s):
    MSG_PUSH=0, MSG_ATTACH=1, MSG_DETACH=2, MSG_WINCH=3, MSG_REDRAW=4
- Screen rendering via pyte
- Per-socket flock concurrency control (<sock>.lock)
- Multi-agent state classifier (claude, agy, gemini, copilot, opencode, bash)
- Session lifecycle: spawn, send, keys, read, wait, list_sessions, kill
"""

from __future__ import annotations

import contextlib
import fcntl
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
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, Union

from ._db import get_workspace, mark_session
from ._proc import reap_in_background

# dtach wire protocol constants
MSG_PUSH = 0
MSG_ATTACH = 1
MSG_DETACH = 2
MSG_WINCH = 3
MSG_REDRAW = 4

_PKT = struct.Struct("<BB8s")
assert _PKT.size == 10
_PUSH_MAX = 8

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


def _packet(ptype: int, data: bytes = b"") -> bytes:
    d = data[:_PUSH_MAX]
    return _PKT.pack(ptype, len(d), d.ljust(_PUSH_MAX, b"\x00"))


def _winch_packet(rows: int, cols: int) -> bytes:
    ws = struct.pack("<HHHH", rows, cols, 0, 0)
    return _PKT.pack(MSG_WINCH, 0, ws)


# Public aliases: downstream consumers import these; private names kept.
PKT = _PKT
PUSH_MAX = _PUSH_MAX
packet = _packet
winch_packet = _winch_packet


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


@contextlib.contextmanager
def sock_lock(sock: str, timeout: float = 10.0):
    """Acquire flock on {sock}.lock."""
    lock_file = f"{sock}.lock"
    fh = open(lock_file, "a")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                raise TimeoutError(f"lock timeout for {sock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _probe_connect(path: str, timeout: float = 0.5) -> bool:
    """Check if socket is currently accepting connections."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


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


def _pid_argv_has(pid: int, token: str) -> bool:
    """True if ``token`` is an exact argv element of ``pid`` (PID-reuse guard)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return token.encode() in f.read().split(b"\0")
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
        return token in shlex.split(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


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
            if argv and os.path.basename(argv[0]) == "dtach" and sock_path in argv:
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
    """macOS/BSD fallback: parse ``ps -axo pid=,ppid=,command=`` (no pgrep needed)."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
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
        if argv and os.path.basename(argv[0]) == "dtach" and sock_path in argv:
            matches.append(pid)
    with_children = [p for p in matches if p in parents]
    return with_children[0] if len(with_children) == 1 else None


def find_dtach_master_pid(sock_path: str) -> Optional[int]:
    """PID of the process that OWNS ``sock_path`` (its master, not a client).

    Order: (1) live ``<sock>.pid`` sidecar (written by spawn() and the stdlib
    fallback daemon); (2) Linux ``/proc`` scan; (3) ``ps`` parse when ``/proc``
    is absent (macOS/BSD). The scans pick processes whose argv[0] basename is
    ``dtach`` and whose argv carries ``sock_path`` as an EXACT token; only the
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
    s.sendall(_PKT.pack(1, 0, b'\\x00'*8))
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


DEFAULT_OPENCODE_MODEL = "opencode/big-pickle"
OPENCODE_FREE_MODELS = (
    "opencode/big-pickle",
    "opencode/mimo-v2.6-flash-free",
    "opencode/glm-5.3-flash-free",
    "opencode/deepseek-v4.1-flash-free",
    "opencode/beam-free",
    "opencode/flint-40b-free",
    "opencode/nemotron-3.5-lightning-free",
    "opencode/nemotron-3.5-super-vl-free",
)


def _is_exec_file(p: Union[str, Path]) -> bool:
    """Return True if p is an existing executable file, ignoring PermissionError on restricted paths."""
    try:
        return os.path.isfile(str(p)) and os.access(str(p), os.X_OK)
    except OSError:
        return False


def find_opencode_bin() -> Optional[str]:
    """Locate the opencode CLI binary, preferring ~/.opencode/bin (v2 installer) over global PATH."""
    for candidate in (
        Path.home() / ".opencode" / "bin" / "opencode",
        Path("/root/.opencode/bin/opencode"),
    ):
        if _is_exec_file(candidate):
            return str(candidate)
    found = shutil.which("opencode")
    if found:
        return found
    if _is_exec_file(Path("/usr/local/bin/opencode")):
        return "/usr/local/bin/opencode"
    return None


def _parse_opencode_model(cmd: Union[str, list[str]]) -> Optional[str]:
    """
    If cmd specifies an opencode agent invocation, return the target model ID
    (defaulting to DEFAULT_OPENCODE_MODEL = 'opencode/big-pickle').
    Otherwise return None.
    """
    if isinstance(cmd, list):
        if not cmd:
            return None
        first = os.path.basename(cmd[0].strip())
        if first != "opencode":
            return None
        for i, tok in enumerate(cmd[1:], start=1):
            if tok in ("-m", "--model") and i + 1 < len(cmd):
                return cmd[i + 1]
            if tok.startswith("--model="):
                return tok.split("=", 1)[1]
        return DEFAULT_OPENCODE_MODEL

    if isinstance(cmd, str):
        stripped = cmd.strip()
        if stripped == "opencode":
            return DEFAULT_OPENCODE_MODEL
        if stripped.startswith("opencode/"):
            # Shorthand like "opencode/big-pickle" or "opencode/mimo-v2.6-flash-free"
            if stripped == "opencode/minimax-m2.5-free":
                return "opencode/mimo-v2.6-flash-free"
            return stripped
        if stripped.startswith("opencode:"):
            model_part = stripped.split(":", 1)[1].strip()
            return model_part if "/" in model_part else f"opencode/{model_part}"
        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = stripped.split()
        if parts and os.path.basename(parts[0]) == "opencode":
            # Only normalize if not already a raw 'opencode run ...' command with a message
            if len(parts) >= 2 and parts[1] == "run":
                return None
            for i, tok in enumerate(parts[1:], start=1):
                if tok in ("-m", "--model") and i + 1 < len(parts):
                    return parts[i + 1]
                if tok.startswith("--model="):
                    return tok.split("=", 1)[1]
            return DEFAULT_OPENCODE_MODEL
    return None


CLAUDE_DECLUTTER_SETTINGS: dict[str, object] = {
    "prefersReducedMotion": True,
    "spinnerTipsEnabled": False,
    "terminalProgressBarEnabled": False,
}
AGY_DECLUTTER_SETTINGS: dict[str, object] = {
    "altScreenMode": False,
    "showTips": False,
    "showFeedbackSurvey": False,
    "artifactReviewPolicy": "always-proceed",
}
COPILOT_SCREEN_READER_ARGS: tuple[str, ...] = (
    "--screen-reader",
    "--plain-diff",
    "--no-mouse",
    "--no-auto-update",
)
GEMINI_SCREEN_READER_ARGS: tuple[str, ...] = ("--screen-reader",)

_AGENT_BINS = ("claude", "agy", "copilot", "gemini")


def _settings_arg(settings: dict[str, object]) -> str:
    return shlex.quote(json.dumps(settings, separators=(",", ":")))


def _normalize_known_agent(
    cmd: Union[str, list[str]], mode: str, prompt: Optional[str], declutter: bool
) -> Optional[str]:
    """Build the command for claude/agy/copilot/gemini, or None if `cmd` is not one of them."""
    try:
        toks = list(cmd) if isinstance(cmd, list) else shlex.split(cmd)
    except ValueError:
        return None
    if not toks or toks[0] not in _AGENT_BINS:
        return None
    bin_ = toks[0]
    has = set(toks)
    out = list(toks)

    def add(*flags: str) -> None:
        for f in flags:
            if f not in has:
                out.append(f)
                has.add(f)

    extra: list[str] = []
    if bin_ in ("claude", "agy"):
        if bin_ == "agy" or "--permission-mode" not in has:
            add("--dangerously-skip-permissions")
        if declutter:
            if bin_ == "claude":
                if os.environ.get("NEXUS_CLAUDE_SCREEN_READER") != "0":
                    add("--ax-screen-reader")
                if "--settings" not in has:
                    extra += ["--settings", _settings_arg(CLAUDE_DECLUTTER_SETTINGS)]
            elif "--settings" not in has:
                extra += ["--settings", _settings_arg(AGY_DECLUTTER_SETTINGS)]
    elif bin_ == "copilot":
        add("--allow-all-tools")
        if declutter and os.environ.get("NEXUS_COPILOT_SCREEN_READER") != "0":
            add(*COPILOT_SCREEN_READER_ARGS)
    else:  # gemini
        if "--yolo" not in has:
            add("-y")
        if (declutter and mode == "pty" and os.environ.get("NEXUS_GEMINI_SCREEN_READER") != "0"):
            add(*GEMINI_SCREEN_READER_ARGS)
    line = " ".join([shlex.quote(t) for t in out] + extra)
    if mode != "pty" and prompt is not None:
        line += " -p " + shlex.quote(prompt)
    return line


def normalize_agent_cmd(
    cmd: Union[str, list[str]],
    mode: str = "pty",
    prompt: Optional[str] = None,
    declutter: Optional[bool] = None,
) -> Union[str, list[str]]:
    """
    Normalize agent command for pty or oneshot execution.
    - claude/agy/copilot/gemini get their auto-approve flag; in pty mode (declutter default on,
      opt out with NEXUS_LITE_PTY_DECLUTTER=0) also screen-reader / declutter flags so TUI
      animations, borders and alt-screen noise do not reach the orchestrator.
      Oneshot mode appends "-p <prompt>" and declutters only if declutter=True.
    - Expands "opencode" (or "opencode -m <model>" / "opencode/<model>") to use
      OpenCode's built-in zero-signup free models (default: opencode/big-pickle) via
      "opencode run --standalone --auto -m <model> <prompt>" in oneshot mode.
    """
    if declutter is None:
        declutter = mode == "pty" and os.environ.get("NEXUS_LITE_PTY_DECLUTTER", "1") != "0"
    known = _normalize_known_agent(cmd, mode, prompt, declutter)
    if known is not None:
        return known

    opencode_model = _parse_opencode_model(cmd)
    if opencode_model is not None:
        oc_bin = shlex.quote(find_opencode_bin() or "opencode")
        q_model = shlex.quote(opencode_model)
        if mode == "pty":
            return f"{oc_bin} -m {q_model}"
        if prompt is not None:
            return f"{oc_bin} run --standalone --auto -m {q_model} {shlex.quote(prompt)}"
        return f"{oc_bin} run --standalone --auto -m {q_model}"

    return cmd


def spawn(
    cmd: Union[str, list[str]],
    name: Optional[str] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> str:
    """Launch a command inside a persistent dtach PTY session and return session name."""
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
            for sibling in (f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark", f"{sp}.pid", f"{sp}.pgid"):
                if os.path.exists(sibling):
                    try:
                        os.unlink(sibling)
                    except OSError:
                        pass

    cmd_str = " ".join(shlex.quote(a) for a in cmd) if isinstance(cmd, list) else str(cmd)
    shell = os.environ.get("SHELL", "/bin/bash")
    cd_prefix = f"cd {shlex.quote(str(cwd))} && " if cwd else ""
    inner = f"{cd_prefix}{cmd_str}"
    use_py = _use_python_dtach()
    if use_py:
        argv = [sys.executable, "-m", "nexus_lite.pty", "--serve", sp, "--", shell, "-lc", inner]
    else:
        argv = ["dtach", "-n", sp, "-z", "-r", "winch", shell, "-lc", inner]

    try:
        reap_in_background(subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        ))
    except OSError as e:
        raise RuntimeError(f"Failed to launch dtach: {e}") from e

    # Await socket availability
    start = time.monotonic()
    sock_ready = False
    while time.monotonic() - start < 4.0:
        if os.path.exists(sp) and _probe_connect(sp):
            sock_ready = True
            break
        time.sleep(0.05)

    if not sock_ready:
        raise RuntimeError(f"dtach failed to create socket {sp} within 4 seconds")

    if not use_py:  # the fallback daemon writes its own sidecar
        mp = _scan_proc(sp) if os.path.isdir("/proc") else _scan_ps(sp)
        if mp:
            _write_pid_sidecar(sp, mp)
    _record_pgid(sp)

    # Set initial terminal geometry (240 cols x 45 rows)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(sp)
            s.sendall(_packet(MSG_ATTACH))
            s.sendall(_winch_packet(CAPTURE_ROWS, CAPTURE_COLS))
            s.sendall(_packet(MSG_DETACH))
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
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        try:
            s.connect(sp)
            # Push data in 8-byte chunks
            for i in range(0, len(data), _PUSH_MAX):
                chunk = data[i : i + _PUSH_MAX]
                s.sendall(_packet(MSG_PUSH, chunk))
            if enter:
                time.sleep(0.05)
                s.sendall(_packet(MSG_PUSH, b"\r"))
        finally:
            try:
                s.close()
            except OSError:
                pass
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
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        try:
            s.connect(sp)
            for token in tokens:
                b = key_to_bytes(token)
                for i in range(0, len(b), _PUSH_MAX):
                    chunk = b[i : i + _PUSH_MAX]
                    s.sendall(_packet(MSG_PUSH, chunk))
                time.sleep(0.02)
        finally:
            try:
                s.close()
            except OSError:
                pass


def read(name: str, cols: int = CAPTURE_COLS, rows: int = CAPTURE_ROWS, settle: float = 0.25) -> str:
    """Read and render session screen text using pyte."""
    import pyte

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
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            s.connect(sp)
            s.sendall(_packet(MSG_ATTACH))
            # Nudge the size so a redraw happens even when it equals the current geometry.
            s.sendall(_winch_packet(rows + 1, cols))
            time.sleep(0.1)
            s.sendall(_winch_packet(rows, cols))
            s.setblocking(False)
            t0 = time.monotonic()
            extra = bytearray()
            while time.monotonic() - t0 < 0.3:
                try:
                    ch = s.recv(4096)
                    if not ch:
                        break
                    extra.extend(ch)
                except (BlockingIOError, InterruptedError):
                    time.sleep(0.02)
                except OSError:
                    break
            try:
                s.sendall(_packet(MSG_DETACH))
            except OSError:
                pass
            raw = bytes(extra)
        except Exception:
            pass
        finally:
            try:
                s.close()
            except OSError:
                pass

    if not raw:
        return ""

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(raw.decode("utf-8", "replace"))

    lines = [ln.rstrip() for ln in screen.display]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


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
                for sibling in (f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark", f"{sp}.pid", f"{sp}.pgid"):
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

    for path in (sp, f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark", f"{sp}.pid", f"{sp}.pgid"):
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
# Stdlib dtach fallback: used when the dtach binary is absent (or
# NEXUS_LITE_DTACH=python). Speaks the same <BB8s protocol as the client code.
# ---------------------------------------------------------------------------

def _use_python_dtach() -> bool:
    return (os.environ.get("NEXUS_LITE_DTACH", "").lower() == "python"
            or shutil.which("dtach") is None)


def _serve(sock: str, argv: list[str]) -> None:
    """Detach (double-fork), then run ``argv`` in a PTY and serve ``sock``.

    Writes ``<sock>.pid``; exits when the child exits, on SIGTERM, or when
    ``sock`` is unlinked."""
    import select
    import signal
    import termios

    if os.fork():
        return
    os.setsid()
    if os.fork():
        os._exit(0)

    pidf = f"{sock}.pid"
    Path(pidf).write_text(f"{os.getpid()}\n")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(sock)
        srv.listen(8)
        st0 = os.stat(sock)
        ident = (st0.st_dev, st0.st_ino)

        def _same_sock() -> bool:
            try:
                st = os.stat(sock)
            except OSError:
                return False
            return (st.st_dev, st.st_ino) == ident
        child, mfd = os.forkpty()
        if child == 0:
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        fcntl.ioctl(mfd, termios.TIOCSWINSZ, struct.pack("<HHHH", 24, 80, 0, 0))

        def _term(*_a):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, _term)

        clients: dict[socket.socket, list] = {}  # sock -> [attached, inbuf]
        done = False
        while not done and _same_sock():
            rl, _, _ = select.select([srv, mfd, *clients], [], [], 0.5)
            for r in rl:
                if r is srv:
                    c, _ = srv.accept()
                    c.setblocking(False)
                    clients[c] = [False, b""]
                elif r is mfd:
                    try:
                        data = os.read(mfd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        done = True
                        break
                    for c, st in list(clients.items()):
                        if st[0]:
                            try:
                                c.send(data)
                            except OSError:
                                pass
                else:
                    try:
                        chunk = r.recv(4096)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        clients.pop(r).clear()
                        r.close()
                        continue
                    st = clients[r]
                    st[1] += chunk
                    while len(st[1]) >= _PKT.size:
                        ptype, ln, payload = _PKT.unpack(st[1][:_PKT.size])
                        st[1] = st[1][_PKT.size:]
                        if ptype == MSG_PUSH:
                            os.write(mfd, payload[:ln])
                        elif ptype == MSG_ATTACH:
                            st[0] = True
                        elif ptype == MSG_DETACH:
                            st[0] = False
                        elif ptype == MSG_WINCH:
                            fcntl.ioctl(mfd, termios.TIOCSWINSZ, payload)
                            try:
                                os.killpg(child, signal.SIGWINCH)
                            except OSError:
                                pass
                        elif ptype == MSG_REDRAW:
                            try:
                                os.write(mfd, b"\x0c")
                            except OSError:
                                pass
            if not done and os.waitpid(child, os.WNOHANG)[0]:
                done = True
    except SystemExit:
        pass
    finally:
        try:
            os.killpg(child, signal.SIGHUP)
        except (OSError, NameError, UnboundLocalError):
            pass
        try:
            if _same_sock():
                os.unlink(sock)
        except (OSError, NameError, UnboundLocalError):
            pass
        try:
            if Path(pidf).read_text().strip() == str(os.getpid()):
                os.unlink(pidf)
        except OSError:
            pass
        os._exit(0)


if __name__ == "__main__":
    _args = sys.argv[1:]
    if len(_args) >= 4 and _args[0] == "--serve" and _args[2] == "--":
        _serve(_args[1], _args[3:])
    else:
        sys.exit("usage: python -m nexus_lite.pty --serve <sock> -- <cmd...>")
