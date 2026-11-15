"""
nexus_lite.pty.server — the persistent PTY session server (stdlib only, POSIX).

A session is one command running on a pseudo-terminal, owned by a small detached server process that listens on a
Unix socket. Any number of clients may connect at the same time; each one can type into the program, receive its
output while attached, resize the terminal and ask for a redraw. The program keeps running when every client is gone.

Clients speak the dtach 0.9 packet format (see :mod:`nexus_lite.pty.protocol`), so existing dtach clients
(nexus-lite's own, nexus.code's dtach_ctl, ``dtach -a``) work against this server unchanged.

Semantics:
- PUSH writes the bytes to the program's terminal input.
- ATTACH / DETACH turn output delivery to that client on / off (output produced while nobody is attached is
  discarded, as with dtach). The socket file gets the owner execute bit while at least one client is attached
  (mode 0700) and loses it when none is (0600). With the ``winch`` redraw method (the session default chosen at
  creation), ATTACH itself sends SIGWINCH to the foreground job (dtach 0.9 ``-r winch``): no REDRAW packet needed.
- WINCH sets the terminal size (the kernel signals SIGWINCH to the foreground job when the size changes).
- REDRAW sets the size, then by method: none = nothing (not even the size), ctrl_l = type ``^L`` if the program has
  the terminal in no-echo, non-canonical, one-byte-read mode, winch = send SIGWINCH to the foreground job even if the
  size did not change.
- The session ends when the program exits (or its terminal closes, or the server gets SIGTERM/SIGINT, or its socket
  file is removed or replaced). Attached clients then see EOF, the socket file is removed and the program's process
  group gets SIGHUP. The program is reaped by the server, so it never lingers as a zombie.

Process layout: :func:`create` binds the socket (so it exists when it returns), forks twice and returns once the
program has been exec'ed; the server is a grandchild in its own session (never a session leader, so it can never
acquire a controlling terminal); its one child is the program, which leads its own session and process group (so
``killpg(<program pid>)`` reaches every process of the session). The server writes its pid to ``<sock>.pid``.
:func:`serve` is the same server in the calling process (dtach ``-N``): no fork, the exit code is the program's.

The server's argv always carries SOCK as an exact token (``python -m nexus_lite.pty serve ... SOCK -- CMD``), which
process-table scans use to find it.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import socket
import stat
import sys
import termios
import time
from typing import Optional, Sequence

from .protocol import (
    MSG_ATTACH, MSG_DETACH, MSG_PUSH, MSG_REDRAW, MSG_SIDECAR, MSG_WINCH, PKT, PKT_SIZE, PUSH_MAX, REDRAW_CTRL_L,
    REDRAW_NONE, REDRAW_WINCH, file_ident, probe_connect, set_nonblocking, sock_lock,
)

_READ_CHUNK = 65536
_CLIENT_HIGH_WATER = 256 << 10  # stop reading the program while every attached client has this much queued
_CLIENT_HARD_CAP = 8 << 20      # a client that falls this far behind is disconnected (it can re-attach and redraw)
_INPUT_HIGH_WATER = 1 << 20     # stop reading clients while this much input waits for the program
_IDENT_CHECK_S = 1.0            # how often the server checks that its socket file is still its own
_DRAIN_S = 0.5                  # after the program exits: how long to keep collecting its last output

EXIT_EXEC_FAILED = 127


class SessionExists(Exception):
    """The socket is owned by a live session."""


# ---------------------------------------------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------------------------------------------

class _Client:
    __slots__ = ("sock", "attached", "sidecar", "inbuf", "outbuf")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.attached = False
        self.sidecar = False  # an automatic observer (MSG_SIDECAR): not counted as a person attached
        self.inbuf = bytearray()
        self.outbuf = bytearray()


class _Server:
    """Event loop of one session: the listening socket, its clients, the PTY master and the program."""

    def __init__(self, listener: socket.socket, path: str, ident: tuple, mfd: int, child: int,
                 redraw: int) -> None:
        self.listener = listener
        self.path = path
        self.ident = ident
        self.mfd = mfd
        self.child = child
        self.redraw = redraw
        self.clients: dict = {}  # socket -> _Client
        self.to_pty = bytearray()
        self.child_done = False
        self.child_status: Optional[int] = None  # raw wait status of the program once reaped
        self.pty_closed = False
        self.stop = False
        self.stop_signal = 0
        self.ended_by_stop = False  # stopped (signal, socket gone) while the program was still running
        self.any_attached = False
        self.wake_r, self.wake_w = os.pipe()

    # --- signals -------------------------------------------------------------------------------------------
    def install_signals(self) -> None:
        for s in (signal.SIGHUP, signal.SIGPIPE, signal.SIGTTIN, signal.SIGTTOU, signal.SIGXFSZ):
            signal.signal(s, signal.SIG_IGN)

        def _stop(signum, _frame) -> None:
            self.stop = True
            self.stop_signal = signum

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGCHLD, lambda *_a: None)  # the wakeup fd interrupts select; reaping is in the loop
        set_nonblocking(self.wake_r)
        set_nonblocking(self.wake_w)
        signal.set_wakeup_fd(self.wake_w, warn_on_full_buffer=False)

    def _reap(self) -> None:
        """Collect every exited child without blocking (the program is our only child)."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                self.child_done = True
                return
            if pid == 0:
                return
            if pid == self.child:
                self.child_done = True
                self.child_status = status

    # --- clients -------------------------------------------------------------------------------------------
    def _accept(self) -> None:
        while True:
            try:
                s, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            s.setblocking(False)
            self.clients[s] = _Client(s)

    def _drop(self, c: _Client) -> None:
        self.clients.pop(c.sock, None)
        try:
            c.sock.close()
        except OSError:
            pass
        self._update_mode()

    def _update_mode(self) -> None:
        """Owner-execute bit on the socket file mirrors 'at least one client attached'."""
        attached = any(c.attached and not c.sidecar for c in self.clients.values())
        if attached == self.any_attached:
            return
        self.any_attached = attached
        if file_ident(self.path) != self.ident:
            return
        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
            os.chmod(self.path, (mode | stat.S_IXUSR) if attached else (mode & ~stat.S_IXUSR))
        except OSError:
            pass

    def _client_readable(self, c: _Client) -> None:
        try:
            data = c.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            self._drop(c)
            return
        c.inbuf += data
        n = len(c.inbuf) - len(c.inbuf) % PKT_SIZE
        for off in range(0, n, PKT_SIZE):
            ptype, length, payload = PKT.unpack_from(c.inbuf, off)
            self._handle(c, ptype, length, payload)
        del c.inbuf[:n]

    def _handle(self, c: _Client, ptype: int, length: int, payload: bytes) -> None:
        if ptype == MSG_PUSH:
            if length <= PUSH_MAX:
                if c.attached and not c.sidecar:  # an interactive client; control.send/keys never ATTACH
                    self._stamp_input()
                self._input(payload[:length])
        elif ptype == MSG_ATTACH:
            c.attached = True
            self._update_mode()
            if self.redraw == REDRAW_WINCH:  # dtach 0.9 -r winch: attaching alone repaints the program
                self._signal_foreground(signal.SIGWINCH)
        elif ptype == MSG_SIDECAR:
            c.sidecar = True
            self._update_mode()
        elif ptype == MSG_DETACH:
            c.attached = False
            self._update_mode()
        elif ptype == MSG_WINCH:
            self._set_size(payload)
        elif ptype == MSG_REDRAW:
            method = length or self.redraw
            if method == REDRAW_NONE:
                return
            self._set_size(payload)
            if method == REDRAW_CTRL_L:
                if self._char_at_a_time():
                    self._input(b"\x0c")
            elif method == REDRAW_WINCH:
                self._signal_foreground(signal.SIGWINCH)
        # any other type: ignored

    def _stamp_input(self) -> None:
        """Record "a human typed just now": the epoch time in ``<sock>.input`` (atomic write, mode 0600)."""
        tmp = f"{self.path}.input.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, f"{time.time():.3f}\n".encode())
            finally:
                os.close(fd)
            os.replace(tmp, f"{self.path}.input")
        except OSError:
            pass

    def _client_writable(self, c: _Client) -> None:
        try:
            n = c.sock.send(c.outbuf)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(c)
            return
        del c.outbuf[:n]

    def _broadcast(self, data: bytes) -> None:
        for c in [c for c in self.clients.values() if c.attached]:
            if not c.outbuf:
                try:
                    n = c.sock.send(data)
                except (BlockingIOError, InterruptedError):
                    n = 0
                except OSError:
                    self._drop(c)
                    continue
                if n < len(data):
                    c.outbuf += data[n:]
            else:
                c.outbuf += data
            if len(c.outbuf) > _CLIENT_HARD_CAP:
                self._drop(c)

    # --- terminal ------------------------------------------------------------------------------------------
    def _input(self, data: bytes) -> None:
        if not data or self.pty_closed:
            return
        if not self.to_pty:
            try:
                n = os.write(self.mfd, data)
            except (BlockingIOError, InterruptedError):
                n = 0
            except OSError:
                return
            data = data[n:]
        self.to_pty += data

    def _pty_writable(self) -> None:
        try:
            n = os.write(self.mfd, self.to_pty)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.to_pty.clear()
            return
        del self.to_pty[:n]

    def _pty_readable(self) -> bool:
        """Read program output and hand it to attached clients. False on EOF / terminal gone."""
        try:
            data = os.read(self.mfd, _READ_CHUNK)
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:  # EIO: the slave side is closed everywhere
            data = b""
        if not data:
            self.pty_closed = True
            return False
        self._broadcast(data)
        return True

    def _set_size(self, ws: bytes) -> None:
        try:
            fcntl.ioctl(self.mfd, termios.TIOCSWINSZ, ws)
        except OSError:
            pass

    def _char_at_a_time(self) -> bool:
        """True when the program reads its terminal raw: no echo, no line editing, one byte per read."""
        try:
            attrs = termios.tcgetattr(self.mfd)
        except termios.error:
            return False
        vmin = attrs[6][termios.VMIN]
        vmin = vmin if isinstance(vmin, int) else ord(vmin)
        return not (attrs[3] & (termios.ECHO | termios.ICANON)) and vmin == 1

    def _signal_foreground(self, sig: int) -> None:
        try:
            pgrp = os.tcgetpgrp(self.mfd)
            if pgrp > 0:
                os.killpg(pgrp, sig)
                return
        except OSError:
            pass
        try:
            os.killpg(self.child, sig)
        except OSError:
            pass

    # --- loop ----------------------------------------------------------------------------------------------
    def _want_pty_read(self) -> bool:
        if self.pty_closed:
            return False
        attached = [c for c in self.clients.values() if c.attached]
        # nobody attached: keep reading and discard; otherwise wait while every attached client is backed up
        return not attached or any(len(c.outbuf) < _CLIENT_HIGH_WATER for c in attached)

    def run(self) -> int:
        set_nonblocking(self.mfd)
        self.listener.setblocking(False)
        next_ident_check = time.monotonic() + _IDENT_CHECK_S
        while not self.stop:
            self._reap()
            if self.child_done or self.pty_closed:
                break
            rlist = [self.listener, self.wake_r]
            if len(self.to_pty) < _INPUT_HIGH_WATER:
                rlist += list(self.clients)
            if self._want_pty_read():
                rlist.append(self.mfd)
            wlist = [s for s, c in self.clients.items() if c.outbuf]
            if self.to_pty:
                wlist.append(self.mfd)
            try:
                r, w, _ = select.select(rlist, wlist, [], _IDENT_CHECK_S)
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno == errno.EBADF:  # a client vanished under us; rebuild the sets
                    for s in list(self.clients):
                        if s.fileno() < 0:
                            self._drop(self.clients[s])
                    continue
                raise
            if self.wake_r in r:
                try:
                    while os.read(self.wake_r, 512):
                        pass
                except (BlockingIOError, InterruptedError):
                    pass
            if self.listener in r:
                self._accept()
            for s in r:
                c = self.clients.get(s)
                if c is not None:
                    self._client_readable(c)
            if self.mfd in w and self.to_pty:
                self._pty_writable()
            if self.mfd in r:
                self._pty_readable()
            for s in w:
                c = self.clients.get(s)
                if c is not None and c.outbuf:
                    self._client_writable(c)
            now = time.monotonic()
            if now >= next_ident_check:
                next_ident_check = now + _IDENT_CHECK_S
                if file_ident(self.path) != self.ident:
                    self.stop = True  # socket removed or replaced: nobody can reach this session any more
        self.ended_by_stop = self.stop and not (self.child_done or self.pty_closed)
        if self.child_done and not self.pty_closed:
            self._drain()
        return self._shutdown()

    def _drain(self) -> None:
        """The program exited: pass on what it wrote last, then flush clients briefly."""
        end = time.monotonic() + _DRAIN_S
        while time.monotonic() < end and not self.pty_closed:
            r, _, _ = select.select([self.mfd], [], [], 0.05)
            if not r or not self._pty_readable():
                break
        while time.monotonic() < end:
            pending = [s for s, c in self.clients.items() if c.outbuf]
            if not pending:
                break
            _, w, _ = select.select([], pending, [], max(0.0, end - time.monotonic()))
            for s in w:
                c = self.clients.get(s)
                if c is not None:
                    self._client_writable(c)

    def _shutdown(self) -> int:
        for c in list(self.clients.values()):
            try:
                c.sock.close()
            except OSError:
                pass
        self.clients.clear()
        try:
            self.listener.close()
        except OSError:
            pass
        try:
            os.close(self.mfd)
        except OSError:
            pass
        try:
            os.killpg(self.child, signal.SIGHUP)  # the program's whole session: background jobs too
        except OSError:
            pass
        end = time.monotonic() + 1.0
        while not self.child_done and time.monotonic() < end:
            self._reap()
            if not self.child_done:
                time.sleep(0.02)
        if file_ident(self.path) == self.ident:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        pidf = f"{self.path}.pid"
        try:
            with open(pidf) as f:
                if f.read().strip() == str(os.getpid()):
                    os.unlink(pidf)
        except OSError:
            pass
        try:  # the human-input stamp belongs to this session only
            if file_ident(self.path) in (None, self.ident):
                os.unlink(f"{self.path}.input")
        except OSError:
            pass
        return self.exit_code()

    def exit_code(self) -> int:
        """Exit code to report for the session: 128+N when the server was stopped by signal N while the program was
        still running (0 when stopped for another reason, e.g. its socket was removed); otherwise the program's own
        (128+N when it was killed by signal N); 0 when unknown."""
        if self.ended_by_stop:
            return 128 + self.stop_signal if self.stop_signal else 0
        if self.child_status is not None:
            try:
                code = os.waitstatus_to_exitcode(self.child_status)
            except ValueError:
                return 0
            return code if code >= 0 else 128 - code
        return 0


def _exec_program(argv: Sequence[str], status_w: int) -> None:
    """In the PTY child: restore default signal handling and run the program; report an exec failure."""
    for s in (signal.SIGPIPE, signal.SIGXFSZ, signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGCHLD,
              signal.SIGTTIN, signal.SIGTTOU):
        try:
            signal.signal(s, signal.SIG_DFL)
        except (OSError, ValueError):
            pass
    try:
        os.execvp(argv[0], list(argv))
    except OSError as e:
        msg = f"pty: could not execute {argv[0]}: {e.strerror}\n"
        try:
            os.write(status_w, msg.encode("utf-8", "replace"))
        except OSError:
            pass
    os._exit(127)


def _run_server(listener: socket.socket, path: str, ident: tuple, argv: Sequence[str], redraw: int,
                status_w: int) -> int:
    """Write the pid sidecar, fork the program onto a PTY and run the event loop; return the exit code."""
    try:
        with open(f"{path}.pid", "w") as f:
            f.write(f"{os.getpid()}\n")
    except OSError:
        pass
    try:
        os.unlink(f"{path}.input")  # a previous session of this socket path left its stamp
    except OSError:
        pass
    child, mfd = os.forkpty()
    if child == 0:
        _exec_program(argv, status_w)
    server = _Server(listener, path, ident, mfd, child, redraw)
    server.install_signals()  # before the launcher returns: a SIGTERM from then on always cleans up
    os.close(status_w)
    return server.run()


def _server_main(listener: socket.socket, path: str, ident: tuple, argv: Sequence[str], redraw: int,
                 status_w: int) -> None:
    """Grandchild of the launcher: becomes the session server. Never returns."""
    code = 1
    try:
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        if null > 2:
            os.close(null)
        code = _run_server(listener, path, ident, argv, redraw, status_w)
    except BaseException:
        if file_ident(path) == ident:
            try:
                os.unlink(path)
            except OSError:
                pass
    finally:
        os._exit(code)


def _bind(path: str, exist_ok: bool) -> Optional[socket.socket]:
    """Bind and listen on ``path`` under the per-socket lock. A stale socket file (nothing accepts on it) is
    unlinked first; a live one means a session already owns it: ``exist_ok`` returns None, else SessionExists.
    The lock is released before the caller forks (a forked server must not inherit it) - by then the socket is
    already listening, so a concurrent attach-or-create sees it as live and never makes a second master."""
    with sock_lock(path):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)
        try:
            try:
                listener.bind(path)
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    raise
                if probe_connect(path):
                    listener.close()
                    if exist_ok:
                        return None
                    raise SessionExists(path) from None
                try:
                    if not stat.S_ISSOCK(os.lstat(path).st_mode):
                        raise
                    os.unlink(path)  # stale: the master is gone
                except FileNotFoundError:
                    pass
                listener.bind(path)
        except BaseException:
            listener.close()
            raise
        finally:
            os.umask(old_umask)
        try:
            os.chmod(path, 0o600)  # owner read/write; the execute bit is added while a client is attached
        except OSError:
            pass
        listener.listen(64)
        return listener


def create(path: str, argv: Sequence[str], redraw: int = REDRAW_CTRL_L, exist_ok: bool = False) -> int:
    """Start a detached session running ``argv`` behind socket ``path``.

    Returns 0 once the program has been exec'ed (the socket already accepts connections), or 1 with a message on
    stderr when the socket cannot be created or the program cannot be executed. With ``exist_ok`` a live session
    already behind ``path`` also returns 0 (attach-or-create); without it that is an error. A stale socket file is
    replaced. Call it from a single-threaded process (it forks); library code runs
    ``python -m nexus_lite.pty serve`` instead."""
    if not argv:
        sys.stderr.write("pty: no command given\n")
        return 1
    try:
        listener = _bind(path, exist_ok)
    except SessionExists:
        sys.stderr.write(f"pty: {path}: session already running\n")
        return 1
    except OSError as e:
        sys.stderr.write(f"pty: {path}: {e.strerror or e}\n")
        return 1
    if listener is None:
        return 0
    ident = file_ident(path)
    status_r, status_w = os.pipe()  # close-on-exec: EOF once the program has exec'ed and the server closed its end
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(status_r)
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            _server_main(listener, path, ident, argv, redraw, status_w)
        finally:
            os._exit(1)
    os.close(status_w)
    listener.close()
    os.waitpid(pid, 0)
    chunks = []
    while True:
        try:
            b = os.read(status_r, 4096)
        except InterruptedError:
            continue
        if not b:
            break
        chunks.append(b)
    os.close(status_r)
    if chunks:
        sys.stderr.write(b"".join(chunks).decode("utf-8", "replace"))
        if file_ident(path) == ident:  # the server notices the exit too; make the failure visible right away
            try:
                os.unlink(path)
            except OSError:
                pass
        return 1
    return 0


def serve(path: str, argv: Sequence[str], redraw: int = REDRAW_CTRL_L) -> int:
    """Run the session server in this process (dtach ``-N``): no fork, stdio untouched. Returns the program's exit
    code (128+N when killed by signal N, 127 when it could not be executed, 1 when the socket cannot be created)."""
    if not argv:
        sys.stderr.write("pty: no command given\n")
        return 1
    try:
        listener = _bind(path, exist_ok=False)
    except SessionExists:
        sys.stderr.write(f"pty: {path}: session already running\n")
        return 1
    except OSError as e:
        sys.stderr.write(f"pty: {path}: {e.strerror or e}\n")
        return 1
    ident = file_ident(path)
    status_w = os.dup(2)  # the program's exec failure message goes to our stderr (non-inheritable: closed on exec)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        return _run_server(listener, path, ident, argv, redraw, status_w)
    except BaseException:
        if file_ident(path) == ident:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
