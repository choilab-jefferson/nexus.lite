"""python -m nexus_lite.pty serve|attach: dtach-compatible -A / -N / -c / -z, stale sockets, redraw on attach, shims."""

import os
import pty as stdpty
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus_lite import pty, ptyd
from nexus_lite.pty import cli, client, control, protocol, server

from .test_ptyd import PROG, Client, _alive_not_zombie, _master_pid, _ps_rows, _wait, env, launch  # noqa: F401

pytestmark = pytest.mark.skipif(not hasattr(os, "forkpty"), reason="POSIX pty required")

PYTHON = [sys.executable, "-m", "nexus_lite.pty"]
DETACH = b"\x1c"


class TtyProc:
    """A `python -m nexus_lite.pty ...` process whose stdin/stdout is a real terminal that the test drives."""

    def __init__(self, args, env_extra=None):
        self.master, slave = stdpty.openpty()
        e = dict(os.environ)
        e.update(env_extra or {})
        self.proc = subprocess.Popen(PYTHON + list(args), stdin=slave, stdout=slave, stderr=slave, env=e,
                                     start_new_session=True, close_fds=True)
        # Hold the slave open like the shell of a real terminal does: macOS discards unread output when the
        # last slave descriptor closes, so the final line of a process that exits would otherwise be lost.
        self.slave = slave
        self.buf = b""

    def pump(self, t=0.2):
        end = time.monotonic() + t
        while time.monotonic() < end:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    b = os.read(self.master, 65536)
                except OSError:
                    return
                if not b:
                    return
                self.buf += b

    def write(self, data: bytes):
        os.write(self.master, data)

    def expect(self, text, t=8.0):
        end = time.monotonic() + t
        while time.monotonic() < end and text.encode() not in self.buf:
            self.pump(0.1)
        return text.encode() in self.buf

    def ready_pid(self):
        self.write(b"who\n")
        assert self.expect("READY pid="), self.buf
        return int(self.buf.split(b"READY pid=")[-1].split()[0])

    def wait(self, t=8.0):
        # Keep reading while waiting, as a terminal does: on macOS a TCSADRAIN restore blocks until it is read.
        end = time.monotonic() + t
        while self.proc.poll() is None and time.monotonic() < end:
            self.pump(0.05)
        return self.proc.wait(timeout=max(0.0, end - time.monotonic()))

    def detach(self):
        self.write(DETACH)
        try:
            return self.wait()
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def tty(env):
    procs = []

    def make(args, env_extra=None):
        p = TtyProc(args, env_extra)
        procs.append(p)
        return p

    yield make
    for p in procs:
        p.close()


def _masters(sp):
    """Live master processes whose argv carries the socket (those with a child: the program)."""
    rows = _ps_rows()
    out = []
    for pid, ppid, _pgid, _stat in rows:
        argv = pty.pid_argv(int(pid)) or []
        if sp in argv and "serve" in argv and any(r[1] == pid for r in rows):
            out.append(int(pid))
    return out


# --- flags --------------------------------------------------------------------------------------------------------

def test_serve_creates_a_detached_session_and_accepts_z(env, tmp_path):
    sp = str(tmp_path / "z.sock")
    p = subprocess.run(PYTHON + ["serve", "-z", "-r", "winch", sp, "--", sys.executable, "-c", PROG],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    env.append(sp)
    assert os.path.exists(sp) and Client(sp).ready()
    assert pty.find_dtach_master_pid(sp) == _master_pid(sp)


def test_A_on_a_missing_socket_creates_and_attaches(env, tmp_path, tty):
    sp = str(tmp_path / "a1.sock")
    t = tty(["serve", "-A", sp, "--", sys.executable, "-c", PROG])
    pid = t.ready_pid()
    env.append(sp)
    assert os.path.exists(sp) and len(_masters(sp)) == 1
    assert t.detach() == 0
    assert _alive_not_zombie(pid) and Client(sp).ready()  # the session outlives the attached client


def test_A_on_a_live_socket_attaches_without_a_second_master(env, tmp_path, tty):
    sp = str(tmp_path / "a2.sock")
    assert launch(env, sp).returncode == 0
    first = Client(sp).ready().buf.split(b"READY pid=")[1].split()[0]
    t = tty(["serve", "-A", sp, "--", sys.executable, "-c", PROG])
    assert str(t.ready_pid()) == first.decode()
    assert len(_masters(sp)) == 1
    t.detach()


def test_A_on_a_stale_socket_replaces_it_with_exactly_one_master(env, tmp_path, tty):
    sp = str(tmp_path / "a3.sock")
    assert launch(env, sp).returncode == 0
    old_master = _master_pid(sp)
    old_child = next(int(r[0]) for r in _ps_rows() if r[1] == str(old_master))
    os.kill(old_master, signal.SIGKILL)  # the socket file stays behind with nothing listening
    os.kill(old_child, signal.SIGKILL)
    assert _wait(lambda: not _alive_not_zombie(old_master))
    assert os.path.exists(sp)
    t = tty(["serve", "-A", sp, "--", sys.executable, "-c", PROG])
    pid = t.ready_pid()
    env.append(sp)
    assert pid != old_child
    masters = _masters(sp)
    assert len(masters) == 1 and masters[0] != old_master
    t.detach()


def test_concurrent_A_makes_one_master_and_every_client_reaches_it(env, tmp_path, tty):
    sp = str(tmp_path / "a4.sock")
    procs = [tty(["serve", "-A", sp, "--", sys.executable, "-c", PROG]) for _ in range(5)]
    pids = {p.ready_pid() for p in procs}
    env.append(sp)
    assert len(pids) == 1  # one program behind all five clients
    assert len(_masters(sp)) == 1
    for p in procs:
        p.detach()


def test_c_creates_and_attaches_and_refuses_a_live_session(env, tmp_path, tty):
    sp = str(tmp_path / "c1.sock")
    t = tty(["serve", "-c", sp, "--", sys.executable, "-c", PROG])
    t.ready_pid()
    env.append(sp)
    second = subprocess.run(PYTHON + ["serve", "-c", sp, "--", sys.executable, "-c", PROG], capture_output=True,
                            text=True, timeout=20, stdin=subprocess.DEVNULL)
    assert second.returncode == 1 and "already running" in second.stderr and sp in second.stderr
    assert len(_masters(sp)) == 1
    t.detach()


def test_plain_serve_on_a_live_socket_fails_and_on_a_stale_one_replaces(env, tmp_path):
    sp = str(tmp_path / "s1.sock")
    assert launch(env, sp).returncode == 0
    again = launch(env, sp)
    assert again.returncode == 1 and "already running" in again.stderr
    master = _master_pid(sp)
    os.kill(master, signal.SIGKILL)
    assert _wait(lambda: not _alive_not_zombie(master))
    assert launch(env, sp).returncode == 0  # stale: replaced
    assert Client(sp).ready()


def test_serve_never_unlinks_a_regular_file(env, tmp_path):
    sp = tmp_path / "file.sock"
    sp.write_text("precious")
    p = subprocess.run(PYTHON + ["serve", str(sp), "--", "sh"], capture_output=True, text=True, timeout=20)
    assert p.returncode == 1 and sp.read_text() == "precious"


def test_N_foreground_exits_with_the_program_exit_code(env, tmp_path):
    sp = str(tmp_path / "n1.sock")
    p = subprocess.run(PYTHON + ["serve", "-N", sp, "--", "sh", "-c", "exit 7"], capture_output=True, text=True,
                       timeout=20, stdin=subprocess.DEVNULL)
    assert p.returncode == 7 and not os.path.exists(sp)
    p = subprocess.run(PYTHON + ["serve", "-N", sp, "--", "sh", "-c", "exit 0"], timeout=20,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 0
    p = subprocess.run(PYTHON + ["serve", "-N", sp, "--", "sh", "-c", "kill -9 $$"], timeout=20,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 128 + 9
    p = subprocess.run(PYTHON + ["serve", "-N", sp, "--", "/nonexistent/prog"], capture_output=True, text=True,
                       timeout=20, stdin=subprocess.DEVNULL)
    assert p.returncode == 127 and "could not execute" in p.stderr and not os.path.exists(sp)


def test_N_does_not_fork_and_serves_clients(env, tmp_path):
    sp = str(tmp_path / "n2.sock")
    proc = subprocess.Popen(PYTHON + ["serve", "-N", sp, "--", sys.executable, "-c", PROG],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait(lambda: os.path.exists(sp) and _master_pid(sp) == proc.pid)  # the server IS this process
        c = Client(sp).ready()
        c.push(b"quit\n")
        assert c.eof()
        assert proc.wait(timeout=10) == 0  # program exit code passes through
        assert not os.path.exists(sp) and not os.path.exists(sp + ".pid")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_N_terminated_by_signal_reports_128_plus_signal(env, tmp_path):
    sp = str(tmp_path / "n3.sock")
    proc = subprocess.Popen(PYTHON + ["serve", "-N", sp, "--", "sleep", "30"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait(lambda: os.path.exists(sp) and _master_pid(sp) == proc.pid and
                     any(r[1] == str(proc.pid) for r in _ps_rows()))
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
        assert not os.path.exists(sp)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("flags", [["-N", "-A"], ["-N", "-c"], ["-A", "-c"]])
def test_conflicting_flags_are_a_usage_error(env, tmp_path, flags):
    p = subprocess.run(PYTHON + ["serve", *flags, str(tmp_path / "x.sock"), "--", "sh"], capture_output=True,
                       text=True, timeout=20, stdin=subprocess.DEVNULL)
    assert p.returncode == 2 and "usage:" in p.stderr


def test_usage_errors(env, tmp_path):
    for args in (["serve"], ["serve", str(tmp_path / "x.sock")], ["attach"], ["attach", "-A", "x"], ["serve", "-r", "bogus", "x", "--", "sh"],
                 ["serve", "-q", "x", "--", "sh"]):
        p = subprocess.run(PYTHON + args, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        assert p.returncode == 2 and "usage:" in p.stderr, args


def test_attach_subcommand_and_detach_char(env, tmp_path, tty):
    sp = str(tmp_path / "at.sock")
    assert launch(env, sp).returncode == 0
    t = tty(["attach", "-e", "^X", sp])
    pid = t.ready_pid()
    t.write(b"\x18")
    assert t.wait() == 0
    t.pump(0.3)
    assert b"[detached]" in t.buf
    assert _alive_not_zombie(pid)
    t2 = tty(["attach", "-E", sp])  # no detach character: Ctrl-\ goes to the program
    t2.ready_pid()
    t2.write(b"\x1c")
    time.sleep(0.4)
    assert t2.proc.poll() is None
    t2.write(b"quit\n")
    assert t2.wait() == 0
    t2.pump(0.3)
    assert b"EOF" in t2.buf


def test_attach_to_a_missing_socket_fails(env, tmp_path, tty):
    t = tty(["attach", str(tmp_path / "none.sock")])
    assert t.wait() == 1


# --- redraw -------------------------------------------------------------------------------------------------------

def test_attach_alone_triggers_winch_redraw_with_r_winch(env, tmp_path):
    sp = str(tmp_path / "w1.sock")
    assert launch(env, sp, redraw="winch").returncode == 0
    Client(sp).ready()
    c = Client(sp)  # sends ATTACH only, no REDRAW packet
    assert c.expect("WINCH ")
    c.close()


def test_attach_alone_does_not_redraw_with_other_methods(env, tmp_path):
    for k, method in enumerate(("ctrl_l", "none")):
        sp = str(tmp_path / f"w{k + 2}.sock")
        assert launch(env, sp, redraw=method).returncode == 0
        Client(sp).ready()
        c = Client(sp)
        c.pump(0.6)
        assert b"WINCH" not in c.buf and b"CTRL_L" not in c.buf
        c.close()


# --- legacy forms and compatibility -------------------------------------------------------------------------------

def test_legacy_ptyd_module_and_argv_forms(env, tmp_path):
    sp = str(tmp_path / "l1.sock")
    p = subprocess.run([sys.executable, "-m", "nexus_lite.ptyd", "-r", "winch", sp, "--", sys.executable, "-c", PROG],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    env.append(sp)
    assert Client(sp).ready()
    argv = pty.pid_argv(_master_pid(sp))
    assert sp in argv and "nexus_lite.ptyd" in argv
    assert pty.find_dtach_master_pid(sp) == _master_pid(sp)
    p = subprocess.run([sys.executable, "-m", "nexus_lite.pty", "-r", "winch", str(tmp_path / "l2.sock"), "--", "sh",
                        "-c", "exit 0"], capture_output=True, text=True, timeout=20)  # no `serve` word: legacy create
    assert p.returncode == 0, p.stderr


def test_master_argv_detection_covers_every_launcher_form():
    sock = "/tmp/x.sock"
    assert pty._is_master_argv(["python", "-m", "nexus_lite.pty", "serve", "-r", "winch", sock, "--", "sh"], sock)
    assert pty._is_master_argv(["python", "-m", "nexus_lite.pty", "serve", "-N", sock, "--", "sh"], sock)
    assert pty._is_master_argv(["python", "-m", "nexus_lite.ptyd", "-r", "winch", sock, "--", "sh"], sock)
    assert pty._is_master_argv(["python", "-m", "nexus_lite.pty", "--serve", sock, "--", "sh"], sock)
    assert pty._is_master_argv(["dtach", "-n", sock, "-z", "sh"], sock)
    assert not pty._is_master_argv(["python", "-m", "nexus_lite.pty", "attach", sock], sock)
    assert not pty._is_master_argv(["python", "-m", "nexus_lite.pty", "serve", "-r", "winch", "/tmp/o.sock", "--", "cat",
                                    sock], sock)


def test_ptyd_shim_reexports_the_package():
    assert ptyd.create is server.create and ptyd.attach is client.attach and ptyd.main is cli.main
    assert ptyd.PKT is protocol.PKT and ptyd.packet is protocol.packet and ptyd.parse_detach_char is cli.parse_detach_char
    assert ptyd.MODULE == "nexus_lite.ptyd"


def test_package_keeps_every_former_public_name():
    names = ("spawn send keys read wait kill kill_session list_sessions attach classify is_busy is_idle "
             "normalize_agent_cmd find_opencode_bin sock_path get_sock_dir default_sock_dir session_name "
             "find_dtach_master_pid pid_argv key_to_bytes keys_to_bytes MSG_PUSH MSG_ATTACH MSG_DETACH MSG_WINCH "
             "MSG_REDRAW PKT PUSH_MAX packet winch_packet KEY_NAMES KEY_MAP PROVIDER_MANIFESTS SPINNERS "
             "inject_or_send sock_lock Screen render").split()
    missing = [n for n in names if not hasattr(pty, n)]
    assert not missing, missing
    assert pty.attach is not client.attach  # name-based attach (control) vs path-based (client)


def test_runtime_is_dependency_free():
    code = ("import sys, nexus_lite, nexus_lite.pty, nexus_lite.pty.screen, nexus_lite.pty.cli, nexus_lite.ptyd;"
            "bad=[m for m in ('pyte','wcwidth') if m in sys.modules]; assert not bad, bad")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)})
    assert p.returncode == 0, p.stderr
    toml = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    deps = toml.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "pyte" not in deps


def test_connection_programmatic_client(env, tmp_path):
    sp = str(tmp_path / "cn.sock")
    assert launch(env, sp).returncode == 0
    with client.Connection(sp) as conn:
        conn.attach()
        conn.winch(30, 100)
        conn.push(b"size\n")
        out = b""
        end = time.monotonic() + 5
        while b"SIZE 30 100" not in out and time.monotonic() < end:
            out += conn.recv(0.2)
        assert b"SIZE 30 100" in out
        conn.push(b"quit\n")
        while not conn.eof and time.monotonic() < end + 5:
            conn.recv(0.2)
        assert conn.eof
    with pytest.raises(OSError):
        client.Connection(str(tmp_path / "missing.sock"))


@pytest.mark.skipif(shutil.which("dtach") is None, reason="no dtach binary")
def test_real_dtach_attaches_to_a_lite_server(env, tmp_path):
    sp = str(tmp_path / "dt.sock")
    assert launch(env, sp, redraw="winch").returncode == 0
    master, slave = stdpty.openpty()
    proc = subprocess.Popen([shutil.which("dtach"), "-a", sp, "-e", "^X"], stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True)
    buf = b""

    def read_until(done, t=8.0):
        nonlocal buf
        end = time.monotonic() + t
        while not done() and time.monotonic() < end:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                buf += os.read(master, 65536)

    try:
        os.write(master, b"who\n")
        read_until(lambda: b"READY pid=" in buf)
        assert b"READY pid=" in buf
        os.write(master, b"\x18")
        read_until(lambda: proc.poll() is not None)  # dtach restores the terminal with TCSADRAIN: keep reading
        assert proc.wait(timeout=1) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
        os.close(slave)  # held open like a real terminal's shell (macOS drops unread output on the last close)


def test_long_socket_path_is_verbatim_in_the_master_argv(env, tmp_path):
    """The master is found by an exact argv token: a ~100 byte socket path must never be shortened."""
    room = control.SUN_PATH_MAX - len(str(tmp_path)) - 1 - len(".sock")  # the longest path every platform binds
    assert room > 10
    sp = str(tmp_path / ("s" * room + ".sock"))
    assert len(sp.encode()) == control.SUN_PATH_MAX
    p = subprocess.run(PYTHON + ["serve", "-r", "winch", sp, "--", sys.executable, "-c", PROG],
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    env.append(sp)
    master = _master_pid(sp)
    argv = pty.pid_argv(master)
    assert sp in argv and "serve" in argv
    os.unlink(sp + ".pid")  # without the sidecar the process-table scan must still find it
    assert pty.find_dtach_master_pid(sp) == master


def test_too_long_socket_path_is_a_readable_error(env, tmp_path, tty):
    sp = str(tmp_path / ("s" * 200 + ".sock"))
    for args in (["serve", sp, "--", sys.executable, "-c", PROG], ["serve", "-N", sp, "--", "true"]):
        p = subprocess.run(PYTHON + args, capture_output=True, text=True, timeout=20)
        assert p.returncode == 1 and "too long" in p.stderr and "None" not in p.stderr, (args, p.stderr)
    t = tty(["attach", sp])
    assert t.wait() == 1
    t.pump(0.3)
    assert b"too long" in t.buf and b"None" not in t.buf, t.buf
