"""
nexus_lite.pty.protocol — the dtach 0.9 wire format and small socket helpers (stdlib only, POSIX).

Wire format, client -> server: a stream of fixed 10-byte records ``struct.Struct("<BB8s")``::

    type  u8   PUSH=0 ATTACH=1 DETACH=2 WINCH=3 REDRAW=4 (other values are ignored; SIDECAR=5 is a nexus-lite
               extension that dtach ignores: "this client is an automatic observer, not a person")
    len   u8   PUSH: number of payload bytes used (0..8; larger values drop the record)
               REDRAW: redraw method (0 = session default, 1 = none, 2 = ctrl_l, 3 = winch)
    data  8B   PUSH: input bytes; WINCH/REDRAW: struct winsize (rows, cols, xpixel, ypixel; native u16)

Server -> client: the raw byte stream the program writes to its terminal, sent only while the client is attached.

This is an independent implementation of dtach's public client interface (packet layout, packet types,
documented command-line behaviour, observed black-box behaviour); it is not derived from dtach's source.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import socket
import struct
import time
from typing import Optional

PKT = struct.Struct("<BB8s")
PKT_SIZE = PKT.size  # 10
assert PKT_SIZE == 10
PUSH_MAX = 8

MSG_PUSH = 0
MSG_ATTACH = 1
MSG_DETACH = 2
MSG_WINCH = 3
MSG_REDRAW = 4
MSG_SIDECAR = 5  # nexus-lite extension, ignored by dtach: "I am an automatic observer (output capture), not a person"

REDRAW_UNSPEC = 0
REDRAW_NONE = 1
REDRAW_CTRL_L = 2
REDRAW_WINCH = 3
REDRAW_METHODS = {"none": REDRAW_NONE, "ctrl_l": REDRAW_CTRL_L, "winch": REDRAW_WINCH}

DEFAULT_DETACH_CHAR = b"\x1c"  # Ctrl-\


def packet(ptype: int, data: bytes = b"", length: Optional[int] = None) -> bytes:
    """One wire record. ``length`` defaults to ``len(data)`` (PUSH); REDRAW passes the method there."""
    data = bytes(data[:PUSH_MAX])
    return PKT.pack(ptype, len(data) if length is None else length, data.ljust(PUSH_MAX, b"\0"))


def winsize_bytes(rows: int, cols: int, xpixel: int = 0, ypixel: int = 0) -> bytes:
    return struct.pack("HHHH", rows, cols, xpixel, ypixel)


def winch_packet(rows: int, cols: int) -> bytes:
    """MSG_WINCH record setting the terminal size (``<HHHH`` winsize, little-endian as on the wire)."""
    return PKT.pack(MSG_WINCH, 0, struct.pack("<HHHH", rows, cols, 0, 0))


def set_nonblocking(fd: int) -> None:
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def file_ident(path: str) -> Optional[tuple]:
    """(st_dev, st_ino) of ``path`` or None: tells a socket file apart from a replacement of the same name."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


@contextlib.contextmanager
def sock_lock(sock: str, timeout: float = 10.0):
    """Acquire flock on ``{sock}.lock`` (serialises creation, input injection and attach-or-create)."""
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


def probe_connect(path: str, timeout: float = 0.5) -> bool:
    """True if the socket at ``path`` currently accepts connections (a live session)."""
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
