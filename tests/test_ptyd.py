"""nexus_lite.ptyd: the stdlib PTY session server (no dtach binary) and its dtach-compatible wire protocol."""

import os
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus_lite import _db, pty, ptyd

pytestmark = pytest.mark.skipif(not hasattr(os, "forkpty"), reason="POSIX pty required")

ROOT = str(Path(__file__).resolve().parent.parent)
PKT = struct.Struct("<BB8s")  # written out here on purpose: an independent client of the wire format

# Answers "who" with READY pid=<pid> pgid=<pgid> (once it runs, its SIGWINCH handler is installed), echoes other
# input lines as IN:<line>, reports its terminal size on "size", prints WINCH <rows> <cols> on SIGWINCH,
# switches to raw mode on "raw", exits on "quit".
PROG = r"""
import fcntl, os, signal, struct, sys, termios
def size():
    r, c, _, _ = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8)); return r, c
def out(s):
    os.write(1, (s + "\r\n").encode())
signal.signal(signal.SIGWINCH, lambda *a: out("WINCH %d %d" % size()))
buf = b""
while True:
    try:
        b = os.read(0, 256)
    except InterruptedError:
        continue
    if not b:
        break
    buf += b
    if b"\x0c" in b:
        out("CTRL_L")
    while b"\n" in buf or b"\r" in buf:
        i = min(x for x in (buf.find(b"\n"), buf.find(b"\r")) if x >= 0)
        line, buf = buf[:i].decode(), buf[i + 1:]
        if line == "who":
            out("READY pid=%d pgid=%d" % (os.getpid(), os.getpgid(0)))
        elif line == "size":
            out("SIZE %d %d" % size())
        elif line == "raw":
            a = termios.tcgetattr(0); a[3] &= ~(termios.ECHO | termios.ICANON); a[6][termios.VMIN] = 1
            termios.tcsetattr(0, termios.TCSANOW, a); out("RAWOK")
        elif line == "quit":
            sys.exit(0)
        elif line:
            out("IN:" + line)
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_LITE_HOME", str(tmp_path / "ws"))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.delenv("NEXUS_LITE_DTACH", raising=False)
    monkeypatch.setenv("PYTHONPATH", ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    _db.reset_init()
    started = []
    yield started
    while _OPEN_CLIENTS:
        _OPEN_CLIENTS.pop().close()
    for sp in started:  # only sessions this test created
        pid = _master_pid(sp) or pty.find_dtach_master_pid(sp)
        if pid:
            with _suppress():
                os.kill(pid, signal.SIGKILL)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], OSError)


def _master_pid(sp):
    try:
        return int(Path(sp + ".pid").read_text())
    except (OSError, ValueError):
        return None


def _wait(cond, t=5.0, step=0.05):
    end = time.monotonic() + t
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


def launch(started, sp, argv=None, redraw=None, path_env=None):
    args = [sys.executable, "-m", "nexus_lite.pty", "serve"] + (["-r", redraw] if redraw else []) + [sp, "--"]
    args += argv or [sys.executable, "-c", PROG]
    e = dict(os.environ)
    if path_env is not None:
        e["PATH"] = path_env
    p = subprocess.run(args, capture_output=True, text=True, timeout=20, env=e)
    if p.returncode == 0:
        started.append(sp)
    return p


_OPEN_CLIENTS = []


class Client:
    """A raw <BB8s client written against the documented format, not against nexus_lite.pty."""

    def __init__(self, sp, attach=True):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        _OPEN_CLIENTS.append(self)  # closed by the env fixture even when a test fails midway
        self.s.connect(sp)
        self.s.settimeout(0.05)
        self.buf = b""
        if attach:
            self.send(1)

    def send(self, ptype, length=0, data=b""):
        self.s.sendall(PKT.pack(ptype, length, data.ljust(8, b"\0")))

    def push(self, data):
        for i in range(0, len(data), 8):
            chunk = data[i:i + 8]
            self.send(0, len(chunk), chunk)

    def winch(self, rows, cols, ptype=3, method=0):
        self.send(ptype, method, struct.pack("HHHH", rows, cols, 0, 0))

    def pump(self, t=0.2):
        end = time.monotonic() + t
        while time.monotonic() < end:
            try:
                b = self.s.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return False
            if not b:
                return False
            self.buf += b
        return True

    def ready(self):
        """Wait until the program runs (its output before an attach is discarded, so ask it)."""
        self.push(b"who\n")
        assert self.expect("READY")
        return self

    def expect(self, text, t=5.0):
        end = time.monotonic() + t
        while time.monotonic() < end:
            if text.encode() in self.buf:
                return True
            if not self.pump(0.05) and text.encode() not in self.buf:
                return False
        return text.encode() in self.buf

    def eof(self, t=5.0):
        end = time.monotonic() + t
        while time.monotonic() < end:
            if not self.pump(0.05):
                return True
        return False

    def close(self):
        self.s.close()


def _ps(pid, field):
    return subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def _alive_not_zombie(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = _ps(pid, "stat")
    return bool(st) and not st.startswith("Z")


def _ps_rows():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat="], capture_output=True, text=True).stdout
    return [tuple(line.split()[:4]) for line in out.splitlines() if len(line.split()) >= 4]


# ---------------------------------------------------------------------------------------------------------------


def test_session_survives_launcher_exit(env, tmp_path):
    sp = str(tmp_path / "s1.sock")
    p = launch(env, sp)
    assert p.returncode == 0, p.stderr
    assert os.path.exists(sp)  # the launcher returns with the socket already bound
    master = _wait(lambda: _master_pid(sp))
    assert master and _alive_not_zombie(master)
    assert _ps(master, "ppid") != str(os.getpid())  # detached: not our child
    c = Client(sp)
    c.push(b"hello\n")
    assert c.expect("IN:hello")
    c.close()


def test_two_clients_attached_at_once_and_detach(env, tmp_path):
    sp = str(tmp_path / "s2.sock")
    assert launch(env, sp).returncode == 0
    a, b = Client(sp), Client(sp)
    time.sleep(0.1)
    a.push(b"one\n")
    assert a.expect("IN:one") and b.expect("IN:one")
    b.send(2)  # DETACH: b stays connected but receives nothing
    time.sleep(0.1)
    b.buf = b""
    b.push(b"two\n")  # a detached client can still type
    assert a.expect("IN:two")
    b.pump(0.3)
    assert b.buf == b""
    a.close(), b.close()


def test_no_output_before_attach(env, tmp_path):
    sp = str(tmp_path / "s3.sock")
    assert launch(env, sp).returncode == 0
    c = Client(sp, attach=False)
    c.push(b"quiet\n")
    c.pump(0.4)
    assert c.buf == b""
    c.send(1)
    c.push(b"loud\n")
    assert c.expect("IN:loud")
    assert b"quiet" not in c.buf.replace(b"loud", b"")  # output while detached was not replayed
    c.close()


def test_resize_changes_pty_winsize(env, tmp_path):
    sp = str(tmp_path / "s4.sock")
    assert launch(env, sp).returncode == 0
    c = Client(sp).ready()
    c.winch(33, 111)
    assert c.expect("WINCH 33 111")
    c.push(b"size\n")
    assert c.expect("SIZE 33 111")
    c.close()


def test_redraw_methods(env, tmp_path):
    sp = str(tmp_path / "s5.sock")
    assert launch(env, sp, redraw="winch").returncode == 0
    c = Client(sp).ready()
    c.winch(20, 70)
    assert c.expect("WINCH 20 70")
    c.buf = b""
    c.winch(20, 70)  # same size: no signal
    c.pump(0.4)
    assert b"WINCH" not in c.buf
    c.winch(20, 70, ptype=4, method=0)  # REDRAW, session default (winch): signal even at the same size
    assert c.expect("WINCH 20 70")
    c.buf = b""
    c.winch(25, 90, ptype=4, method=1)  # REDRAW none: nothing at all, not even the size
    c.push(b"size\n")
    assert c.expect("SIZE 20 70")
    c.buf = b""
    c.winch(20, 70, ptype=4, method=2)  # ctrl_l while cooked: no ^L
    c.pump(0.4)
    assert b"CTRL_L" not in c.buf
    c.push(b"raw\n")
    assert c.expect("RAWOK")
    c.winch(20, 70, ptype=4, method=2)  # ctrl_l while raw: ^L typed
    assert c.expect("CTRL_L")
    c.close()


def test_socket_mode_tracks_attached_clients(env, tmp_path):
    sp = str(tmp_path / "s6.sock")
    assert launch(env, sp).returncode == 0
    mode = lambda: stat.S_IMODE(os.stat(sp).st_mode)  # noqa: E731
    assert mode() == 0o600
    c = Client(sp)
    assert _wait(lambda: mode() == 0o700, 2)
    c.send(2)
    assert _wait(lambda: mode() == 0o600, 2)
    c.send(1)
    assert _wait(lambda: mode() == 0o700, 2)
    c.close()  # disconnect counts as detach
    assert _wait(lambda: mode() == 0o600, 2)


def test_wire_protocol_raw_client_edge_cases(env, tmp_path):
    sp = str(tmp_path / "s7.sock")
    assert launch(env, sp).returncode == 0
    c = Client(sp)
    c.send(0, 9, b"zzzzzzzz")  # PUSH with len > 8: dropped
    c.send(42, 3, b"abc")      # unknown type: ignored
    raw = PKT.pack(0, 6, b"split\n".ljust(8, b"\0"))
    c.s.sendall(raw[:3])       # one record split across writes
    time.sleep(0.1)
    c.s.sendall(raw[3:])
    assert c.expect("IN:split")
    assert b"zzzz" not in c.buf
    c.close()


def test_child_exit_removes_socket_and_leaves_no_zombie(env, tmp_path):
    sp = str(tmp_path / "s8.sock")
    assert launch(env, sp).returncode == 0
    master = _master_pid(sp)
    c = Client(sp).ready()
    child = int(c.buf.split(b"READY pid=")[1].split()[0])
    assert child in [int(r[0]) for r in _ps_rows() if r[1] == str(master)]  # the program is the master's child
    c.push(b"quit\n")
    assert c.eof()  # attached client sees EOF
    c.close()
    assert _wait(lambda: not os.path.exists(sp))
    assert _wait(lambda: not os.path.exists(sp + ".pid"))
    assert _wait(lambda: not _alive_not_zombie(master))
    # neither the program nor the master lingers as a zombie
    assert _wait(lambda: not any(r[0] in (str(child), str(master)) for r in _ps_rows()), 5)


def test_child_is_group_leader_and_master_argv_has_socket(env, tmp_path):
    sp = str(tmp_path / "s9.sock")
    assert launch(env, sp).returncode == 0
    master = _master_pid(sp)
    argv = pty.pid_argv(master)  # /proc cmdline on Linux, ps -ww elsewhere: never truncated
    assert sp in argv and "nexus_lite.pty" in argv and "serve" in argv  # spawn() starts `python -m nexus_lite.pty serve`
    # a harness-style scan (exact argv token match over ps); -ww, or ps cuts long commands at the column width
    out = subprocess.run(["ps", "-ww", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    def tokens(cmd):
        try:
            return shlex.split(cmd)
        except ValueError:  # another process's command line with unbalanced quotes
            return cmd.split()
    roots = {int(line.split(None, 1)[0]) for line in out.splitlines()
             if len(line.split(None, 1)) == 2 and sp in tokens(line.split(None, 1)[1])}
    assert master in roots
    assert pty.find_dtach_master_pid(sp) == master
    os.unlink(sp + ".pid")  # without the sidecar the process scan still finds it
    assert pty.find_dtach_master_pid(sp) == master
    Path(sp + ".pid").write_text(f"{master}\n")
    c = Client(sp).ready()
    child = int(c.buf.split(b"READY pid=")[1].split()[0])
    pgid = int(c.buf.split(b"pgid=")[1].split()[0])
    assert child == pgid  # the program leads its own process group
    assert int(_ps(master, "pgid")) != pgid
    c.close()


def test_kill_removes_process_group_and_socket(env, tmp_path):
    marker = tmp_path / "gc.pid"
    name = pty.spawn(f"nohup sleep 300 >/dev/null 2>&1 & echo $! > {marker}; exec cat", name="kg")
    sp = pty.sock_path(name)
    env.append(sp)
    gc = int(_wait(lambda: marker.exists() and marker.read_text().strip()))
    master = _master_pid(sp)
    pgid = int(Path(sp + ".pgid").read_text())
    assert os.getpgid(gc) == pgid
    assert pty.kill_session(name) == []
    assert _wait(lambda: not _alive_not_zombie(gc))
    assert _wait(lambda: not _alive_not_zombie(master))
    assert not os.path.exists(sp)
    assert not [r for r in _ps_rows() if r[2] == str(pgid)]


def test_sigterm_to_master_hangs_up_program(env, tmp_path):
    sp = str(tmp_path / "sa.sock")
    hup, armed = tmp_path / "hup", tmp_path / "armed"
    script = f"trap 'echo x > {hup}; exit 0' HUP; : > {armed}; while :; do sleep 0.1; done"
    assert launch(env, sp, ["/bin/sh", "-c", script]).returncode == 0
    assert _wait(lambda: armed.exists())
    master = _master_pid(sp)
    os.kill(master, signal.SIGTERM)
    assert _wait(lambda: not os.path.exists(sp))
    assert _wait(lambda: hup.exists())
    assert _wait(lambda: not _alive_not_zombie(master))


def test_launch_errors(env, tmp_path):
    sp = str(tmp_path / "sb.sock")
    p = launch(env, sp, ["/nonexistent/program"])
    assert p.returncode == 1 and "could not execute" in p.stderr
    assert _wait(lambda: not os.path.exists(sp))
    assert launch(env, sp).returncode == 0
    p = launch(env, sp)  # socket already in use
    assert p.returncode == 1 and sp in p.stderr


def test_startup_without_dtach_on_path(env, tmp_path, monkeypatch):
    bare = tmp_path / "bin"
    bare.mkdir()
    for tool in ("sh", "cat", "ps", "sleep", "nohup"):
        src = shutil.which(tool)
        if src:
            os.symlink(src, bare / tool)
    monkeypatch.setenv("PATH", str(bare))
    assert shutil.which("dtach") is None
    name = pty.spawn("cat", name="nodtach")
    env.append(pty.sock_path(name))
    pty.send(name, "via-ptyd")
    assert _wait(lambda: "via-ptyd" in pty.read(name, settle=0.1))
    assert pty.kill(name)


def test_old_style_masters_still_recognised():
    sock = "/tmp/x.sock"
    assert pty._is_master_argv(["dtach", "-n", sock, "-z", "sh"], sock)
    assert pty._is_master_argv(["/usr/bin/python3", "-m", "nexus_lite.pty", "--serve", sock, "--", "sh"], sock)
    assert pty._is_master_argv(["python3", "-m", "nexus_lite.ptyd", "-r", "winch", sock, "--", "sh"], sock)
    assert not pty._is_master_argv(["python3", "-m", "nexus_lite.ptyd", "-r", "winch", "/tmp/o.sock", "--",
                                    "cat", sock], sock)  # the socket only in the program's arguments
    assert not pty._is_master_argv(["python", "--sock", sock], sock)


def test_interactive_attach_cli(env, tmp_path):
    name = pty.spawn(f"{shlex.quote(sys.executable)} -c {shlex.quote(PROG)}", name="att")
    sp = pty.sock_path(name)
    env.append(sp)
    pid, fd = os.forkpty()
    if pid == 0:  # a terminal running `nexus-lite pty attach att`
        try:
            os.execv(sys.executable, [sys.executable, "-m", "nexus_lite", "pty", "attach", name])
        finally:
            os._exit(127)
    out = b""

    def read_until(text, t=8.0):
        nonlocal out
        end = time.monotonic() + t
        while time.monotonic() < end and text not in out:
            import select
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    break
        return text in out

    try:
        time.sleep(0.5)
        os.write(fd, b"typed\r")
        assert read_until(b"IN:typed")
        os.write(fd, b"tail\x1cignored")  # Ctrl-\ detaches; keys before it still reach the program
        assert read_until(b"[detached: att]")
        assert b"ignored" not in out
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        with _suppress():
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
    assert pty._probe_connect(sp)  # the session keeps running after the detach
    pty.kill(name)


def test_dtach_binary_client_against_python_master(env, tmp_path):
    """Oracle: the real `dtach -p` client (when installed) can push input into a Python master."""
    if not shutil.which("dtach"):
        pytest.skip("dtach binary not installed")
    sp = str(tmp_path / "sc.sock")
    assert launch(env, sp).returncode == 0
    c = Client(sp)
    subprocess.run(["dtach", "-p", sp], input=b"from-dtach\n", timeout=10, check=True)
    assert c.expect("IN:from-dtach")
    c.close()


def test_module_constants_match_wire_format():
    assert ptyd.PKT.size == 10 and ptyd.PKT.format == PKT.format
    assert (ptyd.MSG_PUSH, ptyd.MSG_ATTACH, ptyd.MSG_DETACH, ptyd.MSG_WINCH, ptyd.MSG_REDRAW) == (0, 1, 2, 3, 4)
    assert (pty.MSG_PUSH, pty.MSG_ATTACH, pty.MSG_DETACH, pty.MSG_WINCH, pty.MSG_REDRAW) == (0, 1, 2, 3, 4)
    assert ptyd.packet(0, b"ab") == pty.packet(0, b"ab")
