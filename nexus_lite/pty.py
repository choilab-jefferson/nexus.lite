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
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Optional, Union

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


def get_sock_dir() -> str:
    """Return socket directory from NEXUS_LITE_SOCK_DIR or default /tmp."""
    d = os.environ.get("NEXUS_LITE_SOCK_DIR", "/tmp")
    os.makedirs(d, exist_ok=True)
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


def _find_dtach_pid(sock_path: str) -> Optional[int]:
    """Find dtach process PID matching sock_path, preferring the master process with children."""
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
        pass

    for pid in matches:
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as f:
                if f.read().split():
                    return pid
        except OSError:
            continue

    return matches[0] if matches else None


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
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
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


def normalize_agent_cmd(
    cmd: Union[str, list[str]],
    mode: str = "pty",
    prompt: Optional[str] = None,
) -> Union[str, list[str]]:
    """
    Normalize agent command for pty or oneshot execution.
    - Expands "agy" or "claude" (with or without "--dangerously-skip-permissions") to
      "<agent> --dangerously-skip-permissions" in pty mode, and
      "<agent> --dangerously-skip-permissions -p " + shlex.quote(prompt) in oneshot mode.
    - Expands "opencode" (or "opencode -m <model>" / "opencode/<model>") to use
      OpenCode's built-in zero-signup free models (default: opencode/big-pickle) via
      "opencode run --standalone --auto -m <model> <prompt>" in oneshot mode.
    """
    agent_bin: Optional[str] = None
    if isinstance(cmd, list):
        if cmd in (["agy"], ["agy", "--dangerously-skip-permissions"]):
            agent_bin = "agy"
        elif cmd in (["claude"], ["claude", "--dangerously-skip-permissions"]):
            agent_bin = "claude"
    elif isinstance(cmd, str):
        stripped = cmd.strip()
        if stripped in ("agy", "agy --dangerously-skip-permissions"):
            agent_bin = "agy"
        elif stripped in ("claude", "claude --dangerously-skip-permissions"):
            agent_bin = "claude"

    if agent_bin:
        if mode == "pty":
            return f"{agent_bin} --dangerously-skip-permissions"
        if prompt is not None:
            return f"{agent_bin} --dangerously-skip-permissions -p " + shlex.quote(prompt)
        return f"{agent_bin} --dangerously-skip-permissions"

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
    if shutil.which("dtach") is None:
        raise RuntimeError("dtach binary not found on PATH. Install via apt-get install dtach")

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
            for sibling in (f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark"):
                if os.path.exists(sibling):
                    try:
                        os.unlink(sibling)
                    except OSError:
                        pass

    cmd_str = " ".join(shlex.quote(a) for a in cmd) if isinstance(cmd, list) else str(cmd)
    shell = os.environ.get("SHELL", "/bin/bash")
    cd_prefix = f"cd {shlex.quote(str(cwd))} && " if cwd else ""
    inner = f"{cd_prefix}{cmd_str}"
    argv = ["dtach", "-n", sp, "-z", "-r", "winch", shell, "-lc", inner]

    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
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

    # Set initial terminal geometry (240 cols x 45 rows)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sp)
        s.sendall(_packet(MSG_ATTACH))
        s.sendall(_winch_packet(CAPTURE_ROWS, CAPTURE_COLS))
        s.sendall(_packet(MSG_DETACH))
        s.close()
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


def is_busy(text: str, provider: str = "") -> bool:
    """Check if screen output indicates the agent or process is busy working."""
    if not text:
        return False
    prov = provider.lower().strip()
    has_spinner = any(s in text for s in SPINNERS)

    if prov == "claude":
        return "esc to interrupt" in text or bool(CLAUDE_BUSY_RE.search(text)) or has_spinner
    elif prov == "agy":
        return (
            "Analyzing…" in text
            or "Generating" in text
            or "esc to cancel" in text
            or has_spinner
        )
    elif prov == "gemini":
        return "Thinking…" in text or "esc to cancel" in text or has_spinner
    elif prov == "copilot":
        return "Working" in text or "esc cancel" in text or has_spinner
    elif prov == "opencode":
        return "Thinking…" in text or has_spinner
    elif prov == "bash":
        return False
    else:
        # Fallback heuristic
        return (
            has_spinner
            or "Thinking…" in text
            or "Analyzing…" in text
            or "esc to interrupt" in text
            or "esc to cancel" in text
            or bool(CLAUDE_BUSY_RE.search(text))
        )


def is_idle(text: str, provider: str = "") -> bool:
    """Check if screen output indicates the agent or process is waiting for input."""
    if not text or not text.strip():
        return False
    if is_busy(text, provider):
        return False

    prov = provider.lower().strip()
    if prov == "claude":
        return "❯" in text or "> " in text
    elif prov == "agy":
        return "accept-edits" in text or "❯" in text or "> " in text or not is_busy(text, prov)
    elif prov == "gemini":
        return "Type your message" in text or "YOLO" in text or "> " in text
    elif prov == "copilot":
        return "Do you want to" in text or "> " in text or not is_busy(text, prov)
    elif prov == "opencode":
        return "Permission required" in text or "> " in text or not is_busy(text, prov)
    elif prov == "bash":
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return False
        last = lines[-1]
        return bool(BASH_PROMPT_RE.search(last)) or last.endswith(("$", "#", "%", ">", "❯"))
    else:
        # General stability fallback
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            last = lines[-1]
            if bool(BASH_PROMPT_RE.search(last)) or "❯" in text or "> " in text:
                return True
        return not is_busy(text, "")


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
            pid = _find_dtach_pid(sp)
            if pid is None:
                # Cleanup stale socket and lock
                try:
                    os.unlink(sp)
                except OSError:
                    pass
                for sibling in (f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark"):
                    if os.path.exists(sibling):
                        try:
                            os.unlink(sibling)
                        except OSError:
                            pass
                continue
        sessions.append({"name": name, "sock": sp, "alive": True})
    return sessions


def kill(name: str) -> bool:
    """Terminate a dtach session and clean up socket artifacts."""
    sp = sock_path(name)
    pid = _find_dtach_pid(sp)
    if pid is not None:
        try:
            os.kill(pid, 15)  # SIGTERM
            time.sleep(0.1)
            os.kill(pid, 9)   # SIGKILL
        except OSError:
            pass

    for path in (sp, f"{sp}.lock", f"{sp}.scrollback", f"{sp}.sendmark"):
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    return True
