"""
nexus_lite.pty.picker — ``nexus-lite pty ls`` and the interactive attach picker (stdlib only, POSIX).

Not a multiplexer: it lists the sessions of the socket directory, lets a human pick one by number or name, attaches
this terminal (``Ctrl-\\`` detaches, ``--ro`` only watches) and, when started without a name, returns to the list
after every detach. Works against sessions served by a ``dtach`` binary too (same wire protocol).
"""

from __future__ import annotations

import os
import stat
import sys
import time
from typing import List, Optional, TextIO

from . import control
from .protocol import REDRAW_WINCH, probe_connect

DETACH_ENV = "NEXUS_PTY_DETACH"
HEADERS = ("NO", "NAME", "PROVIDER", "AGE", "ATTACHED", "ALIVE")


def fmt_age(seconds: float) -> str:
    """``5s``, ``3m``, ``2h``, ``4d`` (largest whole unit)."""
    s = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def sessions_info(now: Optional[float] = None) -> List[dict]:
    """The live sessions of the socket directory, numbered from 1: ``no``, ``name``, ``sock``, ``provider``
    (from the ``<sock>.meta`` that ``spawn`` writes, ``-`` otherwise), ``age`` (seconds), ``attached`` (at least one
    client attached: the owner execute bit of the socket file, set by the server and by dtach), ``alive`` (the
    socket accepts connections)."""
    now = time.time() if now is None else now
    out = []
    for i, s in enumerate(control.list_sessions(), 1):
        sp = s["sock"]
        meta = control.read_meta(sp)
        try:
            st = os.stat(sp)
            attached = bool(st.st_mode & stat.S_IXUSR)
            created = float(meta.get("created") or st.st_ctime)
        except OSError:
            attached, created = False, now
        out.append({"no": i, "name": s["name"], "sock": sp, "provider": meta.get("provider") or "-",
                    "age": max(0.0, now - created), "attached": attached, "alive": probe_connect(sp)})
    return out


def format_table(info: List[dict]) -> str:
    """Aligned text table: NO NAME PROVIDER AGE ATTACHED ALIVE."""
    rows = [HEADERS] + [(str(r["no"]), r["name"], r["provider"], fmt_age(r["age"]),
                         "yes" if r["attached"] else "no", "yes" if r["alive"] else "no") for r in info]
    widths = [max(len(r[c]) for r in rows) for c in range(len(HEADERS))]
    return "\n".join("  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in rows)


def resolve(info: List[dict], token: str) -> dict:
    """The session ``token`` names: its number (1-based, as ``ls`` prints it), else an exact name, else the one
    name containing ``token`` (case-insensitive). Raises ``LookupError`` with a readable message when none or
    several match."""
    token = token.strip()
    if not token:
        raise LookupError("no session given")
    if token.isdigit():
        n = int(token)
        for r in info:
            if r["no"] == n:
                return r
        raise LookupError(f"no session number {n} (there are {len(info)})")
    for r in info:
        if r["name"] == token:
            return r
    hits = [r for r in info if token.lower() in r["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise LookupError(f"no session matches {token!r}")
    raise LookupError(f"{token!r} matches {len(hits)} sessions: " + ", ".join(r["name"] for r in hits))


def parse_pick(text: str) -> tuple:
    """Interpret a picker line: ``("quit", None)`` for q/quit/exit, ``("refresh", None)`` for an empty line or r,
    else ``("pick", token)``."""
    t = text.strip()
    if t.lower() in ("q", "quit", "exit"):
        return ("quit", None)
    if t == "" or t.lower() in ("r", "refresh", "ls"):
        return ("refresh", None)
    return ("pick", t)


def detach_from_env(default: Optional[bytes]) -> Optional[bytes]:
    """The detach key from ``NEXUS_PTY_DETACH`` (``^X`` or one character), else ``default``."""
    from .cli import parse_detach_char
    text = os.environ.get(DETACH_ENV)
    if text:
        parsed = parse_detach_char(text)
        if parsed is not None:
            return parsed
    return default


def attach_session(info: dict, detach_char: Optional[bytes], ro: bool = False) -> int:
    """Attach this terminal to one session of :func:`sessions_info`: hint line, winsize + winch redraw, restore
    the terminal on detach."""
    return control.attach(info["name"], detach_char=detach_char, ro=ro, label=info["name"], redraw=REDRAW_WINCH)


def picker(detach_char: Optional[bytes], ro: bool = False, stdin: Optional[TextIO] = None,
           stdout: Optional[TextIO] = None) -> int:
    """Numbered session list; type a number or a name to attach, ``r`` to refresh, ``q`` to quit. After a detach
    (or when the session ends) the list comes back. Returns 0, or 1 when there is no terminal to ask on."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if not stdin.isatty():
        sys.stderr.write("pty: the picker needs a terminal on stdin (use `pty attach NAME`)\n")
        return 1
    while True:
        info = sessions_info()
        if not info:
            stdout.write("No active sessions.\n")
            stdout.flush()
            return 0
        stdout.write(format_table(info) + "\n")
        stdout.write("attach [number|name], r refresh, q quit: ")
        stdout.flush()
        line = stdin.readline()
        if not line:
            stdout.write("\n")
            return 0
        action, token = parse_pick(line)
        if action == "quit":
            return 0
        if action == "refresh":
            continue
        try:
            target = resolve(info, token)
        except LookupError as e:
            stdout.write(f"{e}\n")
            continue
        attach_session(target, detach_char, ro)
        _settle(target["sock"])


def _settle(sock: str, timeout: float = 2.0) -> None:
    """After an attach returns, give an ended session's server time to unlink its socket, so the relisted
    table does not show a session that just exited (a detach leaves the socket accepting and returns at once)."""
    deadline = time.time() + timeout
    while os.path.exists(sock) and not probe_connect(sock) and time.time() < deadline:
        time.sleep(0.05)


def attach_command(name: Optional[str], detach_char: Optional[bytes], ro: bool = False) -> int:
    """``pty attach [name|no] [--ro]``: with a name (a number from ``ls`` or part of a session name) attach
    directly, else run the picker."""
    if name is None:
        return picker(detach_char, ro)
    if "/" in name or name.endswith(".sock"):  # a socket path
        return _attach_path(name, detach_char, ro)
    info = sessions_info()
    try:
        target = resolve(info, name)
    except LookupError as e:
        if not name.isdigit() and os.path.exists(control.sock_path(name)):
            return control.attach(name, detach_char=detach_char, ro=ro, label=name, redraw=REDRAW_WINCH)
        sys.stderr.write(f"pty: {e}\n")
        return 1
    return attach_session(target, detach_char, ro)


def _attach_path(path: str, detach_char: Optional[bytes], ro: bool) -> int:
    from . import client
    return client.attach(os.path.abspath(path), detach_char=detach_char, redraw=REDRAW_WINCH, ro=ro,
                         label=os.path.basename(path))
