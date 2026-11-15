"""
nexus_lite.pty.cli — command line of the PTY primitive (``python -m nexus_lite.pty``), dtach-compatible flags.

::

    python -m nexus_lite.pty serve  [-r none|ctrl_l|winch] [-z] [-N] [-A | -c] [-e CHAR | -E] SOCK -- CMD [ARG...]
    python -m nexus_lite.pty attach [-r none|ctrl_l|winch] [-z] [-e CHAR | -E] SOCK

``serve`` alone creates a detached session (dtach ``-n``). ``-A`` attaches, creating the session when the socket is
missing or stale (never two masters for one socket); ``-c`` creates and attaches; ``-N`` runs the server in the
foreground without forking and exits with the program's exit code; ``-z`` is accepted and does nothing.

The pre-package forms stay valid: ``[-r m] SOCK -- CMD...`` (create) and ``-a SOCK [-e CHAR | -E] [-r m]`` (attach),
with or without the ``serve``/``attach`` word (``python -m nexus_lite.ptyd`` is the same command).
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from .client import attach
from .protocol import DEFAULT_DETACH_CHAR, REDRAW_CTRL_L, REDRAW_METHODS, REDRAW_UNSPEC
from .server import create, serve

USAGE = (
    "usage: python -m nexus_lite.pty serve [-r none|ctrl_l|winch] [-z] [-N] [-A | -c] [-e CHAR | -E] SOCK -- CMD [ARG...]\n"
    "       python -m nexus_lite.pty attach [-r none|ctrl_l|winch] [-z] [-e CHAR | -E] SOCK\n"
)


def parse_detach_char(text: str) -> Optional[bytes]:
    if text.startswith("^") and len(text) == 2:
        return bytes([ord(text[1].upper()) ^ 0x40]) if text[1] != "?" else b"\x7f"
    if len(text) == 1:
        return text.encode()
    return None


def main(args: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if args is None else args)
    mode = "create"
    if args and args[0] in ("serve", "attach"):
        mode = "attach" if args.pop(0) == "attach" else "create"
    flag_attach = flag_create_attach = flag_fg = False
    redraw: Optional[int] = None
    detach: Optional[bytes] = DEFAULT_DETACH_CHAR
    sock: Optional[str] = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a == "-a" and i + 1 < len(args):  # legacy: attach to SOCK
            mode, sock = "attach", args[i + 1]
            i += 2
        elif a == "-r" and i + 1 < len(args) and args[i + 1] in REDRAW_METHODS:
            redraw = REDRAW_METHODS[args[i + 1]]
            i += 2
        elif a == "-e" and i + 1 < len(args) and parse_detach_char(args[i + 1]) is not None:
            detach = parse_detach_char(args[i + 1])
            i += 2
        elif a == "-E":
            detach = None
            i += 1
        elif a == "-A":
            flag_attach = True
            i += 1
        elif a == "-c":
            flag_create_attach = True
            i += 1
        elif a == "-N":
            flag_fg = True
            i += 1
        elif a in ("-z", "-n"):  # -z: no suspend key (lite's attach has none); -n: detached create, the default
            i += 1
        elif not a.startswith("-") and sock is None:
            sock = a
            i += 1
        else:
            sys.stderr.write(USAGE)
            return 2
    if sock is None or (flag_fg and (flag_attach or flag_create_attach)) or (flag_attach and flag_create_attach):
        sys.stderr.write(USAGE)
        return 2
    if mode == "attach":
        if i < len(args) or flag_attach or flag_create_attach or flag_fg:
            sys.stderr.write(USAGE)
            return 2
        return attach(sock, detach, REDRAW_UNSPEC if redraw is None else redraw)
    cmd = args[i:]
    redraw_m = REDRAW_CTRL_L if redraw is None else redraw
    if flag_attach and not cmd:
        # dtach -A without a command can only attach (a missing session is an error)
        return attach(sock, detach, REDRAW_UNSPEC if redraw is None else redraw)
    if not cmd:
        sys.stderr.write(USAGE)
        return 2
    if flag_fg:
        return serve(sock, cmd, redraw_m)
    if flag_attach or flag_create_attach:
        rc = create(sock, cmd, redraw_m, exist_ok=flag_attach)
        if rc != 0:
            return rc
        return attach(sock, detach, REDRAW_UNSPEC if redraw is None else redraw)
    return create(sock, cmd, redraw_m)
