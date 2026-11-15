"""
nexus_lite.pty.client — clients of a session socket (stdlib only, POSIX).

- :func:`attach`: interactive raw-mode attach of this terminal (dtach ``-a``).
- :class:`Connection`: programmatic connection speaking the dtach 0.9 packets, used by the control API
  (``send``, ``keys``, ``read``) and available to callers that drive a session themselves.
"""

from __future__ import annotations

import fcntl
import os
import select
import signal
import socket
import sys
import termios
import time
from typing import Optional

from .protocol import (
    DEFAULT_DETACH_CHAR, MSG_ATTACH, MSG_DETACH, MSG_PUSH, MSG_REDRAW, MSG_WINCH, PUSH_MAX, REDRAW_UNSPEC,
    packet, set_nonblocking, winsize_bytes,
)

_READ_CHUNK = 65536


class Connection:
    """One client connection to a session socket.

    ``Connection(path)`` connects (raises OSError when nothing accepts). ``push`` types bytes into the program
    (split into 8-byte PUSH records), ``attach``/``detach`` turn output delivery on/off for this connection,
    ``winch`` sets the terminal size, ``redraw`` asks for a redraw by method, ``recv`` returns pending output
    (b"" on EOF or timeout without data - check :attr:`eof`). Usable as a context manager."""

    def __init__(self, path: str, timeout: float = 5.0) -> None:
        self.path = path
        self.eof = False
        self._s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._s.settimeout(timeout)
        try:
            self._s.connect(path)
        except OSError:
            self._s.close()
            raise

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._s.close()
        except OSError:
            pass

    def settimeout(self, timeout: Optional[float]) -> None:
        self._s.settimeout(timeout)

    def send_packet(self, data: bytes) -> None:
        self._s.sendall(data)

    def push(self, data: bytes) -> None:
        for i in range(0, len(data), PUSH_MAX):
            self._s.sendall(packet(MSG_PUSH, data[i:i + PUSH_MAX]))

    def attach(self) -> None:
        self._s.sendall(packet(MSG_ATTACH))

    def detach(self) -> None:
        self._s.sendall(packet(MSG_DETACH))

    def winch(self, rows: int, cols: int) -> None:
        self._s.sendall(packet(MSG_WINCH, winsize_bytes(rows, cols), length=0))

    def redraw(self, rows: int, cols: int, method: int = REDRAW_UNSPEC) -> None:
        self._s.sendall(packet(MSG_REDRAW, winsize_bytes(rows, cols), length=method))

    def recv(self, timeout: Optional[float] = None, size: int = 4096) -> bytes:
        """Pending output, waiting up to ``timeout`` seconds (None: the connection timeout). b"" when none arrived
        or the session ended (then :attr:`eof` is True)."""
        if timeout is not None:
            r, _, _ = select.select([self._s], [], [], timeout)
            if not r:
                return b""
        try:
            data = self._s.recv(size)
        except (socket.timeout, BlockingIOError, InterruptedError):
            return b""
        except OSError:
            self.eof = True
            return b""
        if not data:
            self.eof = True
        return data

    def collect(self, seconds: float) -> bytes:
        """Output received over ``seconds`` (stops early at EOF)."""
        out = bytearray()
        end = time.monotonic() + seconds
        while not self.eof:
            left = end - time.monotonic()
            if left <= 0:
                break
            out += self.recv(min(left, 0.05))
        return bytes(out)


# ---------------------------------------------------------------------------------------------------------------
# Interactive client
# ---------------------------------------------------------------------------------------------------------------

def _tty_size(fd: int) -> bytes:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        return winsize_bytes(24, 80)


def _make_raw(attrs: list) -> list:
    a = [list(x) if isinstance(x, list) else x for x in attrs]
    a[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR | termios.IGNCR
              | termios.ICRNL | termios.IXON)
    a[1] &= ~termios.OPOST
    a[2] &= ~(termios.CSIZE | termios.PARENB)
    a[2] |= termios.CS8
    a[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    a[6][termios.VMIN] = 1
    a[6][termios.VTIME] = 0
    return a


def key_name(detach_char: Optional[bytes]) -> str:
    """Human name of a detach key: ``Ctrl-\\`` for 0x1c, ``Ctrl-X`` for other control characters, else the character."""
    if not detach_char:
        return ""
    o = detach_char[0]
    if o < 0x20:
        return "Ctrl-" + chr(o ^ 0x40)
    return "Ctrl-?" if o == 0x7F else detach_char.decode("utf-8", "replace")


def attach(path: str, detach_char: Optional[bytes] = DEFAULT_DETACH_CHAR, redraw: int = REDRAW_UNSPEC, *,
           ro: bool = False, label: Optional[str] = None) -> int:
    """Attach this terminal to the session at ``path`` until the detach character is typed or the session ends.

    The terminal is put in raw mode and every key (Ctrl-C, Ctrl-Z included: there is no suspend key) goes to the
    program, except ``detach_char`` (default Ctrl-\\; None disables it). Returns 0 on detach or session end, 1 when
    the session cannot be reached or stdin is not a terminal.

    ``ro`` (watch mode) never sends PUSH: keys are read but dropped, only the detach key and ``q`` detach. ``label``
    (the session name) turns on the hint lines ``[attached: NAME - KEY to detach]`` and ``[detached: NAME]``."""
    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    if not os.isatty(fd_in):
        sys.stderr.write("pty: attaching requires a terminal on stdin\n")
        return 1
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
    except OSError as e:
        sys.stderr.write(f"pty: {path}: {e.strerror or e}\n")
        s.close()
        return 1
    saved = termios.tcgetattr(fd_in)
    wake_r, wake_w = os.pipe()
    set_nonblocking(wake_r)
    set_nonblocking(wake_w)
    flags = {"winch": False, "stop": False}

    def _on_winch(*_a) -> None:
        flags["winch"] = True

    def _on_stop(*_a) -> None:
        flags["stop"] = True

    old_handlers = {sig: signal.signal(sig, h) for sig, h in (
        (signal.SIGWINCH, _on_winch), (signal.SIGHUP, _on_stop), (signal.SIGTERM, _on_stop),
        (signal.SIGINT, _on_stop))}
    old_wakeup = signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
    message = ""
    try:
        termios.tcsetattr(fd_in, termios.TCSADRAIN, _make_raw(saved))
        if label:
            hint = f"[attached: {label} \u2014 {key_name(detach_char)} to detach]" if detach_char else f"[attached: {label}]"
            if ro:
                hint += " [read-only: q also detaches]"
            os.write(fd_out, (hint + "\r\n").encode())
        s.sendall(packet(MSG_ATTACH))
        s.sendall(packet(MSG_REDRAW, _tty_size(fd_in), length=redraw))
        while True:
            if flags["stop"]:
                message = "[detached]"
                break
            if flags["winch"]:
                flags["winch"] = False
                s.sendall(packet(MSG_WINCH, _tty_size(fd_in), length=0))
            try:
                r, _, _ = select.select([fd_in, s, wake_r], [], [])
            except InterruptedError:
                continue
            if wake_r in r:
                try:
                    while os.read(wake_r, 512):
                        pass
                except (BlockingIOError, InterruptedError):
                    pass
            if s in r:
                try:
                    data = s.recv(_READ_CHUNK)
                except OSError:
                    data = b""
                if not data:
                    message = "[EOF - session ended]"
                    break
                os.write(fd_out, data)
            if fd_in in r:
                data = os.read(fd_in, 1024)
                if not data:
                    message = "[detached]"
                    break
                cut = data.find(detach_char) if detach_char else -1
                if ro and cut < 0 and b"q" in data:
                    cut = data.find(b"q")
                keys = b"" if ro else (data if cut < 0 else data[:cut])
                for i in range(0, len(keys), PUSH_MAX):
                    s.sendall(packet(MSG_PUSH, keys[i:i + PUSH_MAX]))
                if cut >= 0:
                    try:
                        s.sendall(packet(MSG_DETACH))
                    except OSError:
                        pass
                    message = "[detached]"
                    break
    except OSError:
        message = "[EOF - session ended]"
    finally:
        termios.tcsetattr(fd_in, termios.TCSADRAIN, saved)
        signal.set_wakeup_fd(old_wakeup)
        for sig, h in old_handlers.items():
            signal.signal(sig, h)
        for fd in (wake_r, wake_w):
            os.close(fd)
        s.close()
    if label:
        message = f"[detached: {label}]" if message == "[detached]" else f"[ended: {label}]"
    os.write(fd_out, f"\r\n{message}\r\n".encode())
    return 0


