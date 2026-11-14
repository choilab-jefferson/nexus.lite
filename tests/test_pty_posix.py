"""POSIX dtach support: pid sidecar, ps fallback, stdlib dtach daemon."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from nexus_lite import pty as P

pytestmark = pytest.mark.skipif(not hasattr(os, "forkpty"), reason="POSIX pty required")


@pytest.fixture
def py_dtach(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_LITE_DTACH", "python")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("NEXUS_LITE_HOME", str(tmp_path / "ws"))
    yield tmp_path


def _wait(cond, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_sidecar_lookup(tmp_path):
    sock = str(tmp_path / "x.sock")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", sock])
    try:
        (tmp_path / "x.sock.pid").write_text(f"{proc.pid}\n")
        assert _wait(lambda: P.find_dtach_master_pid(sock) == proc.pid)
    finally:
        proc.kill()
        proc.wait()
    (tmp_path / "x.sock.pid").write_text("999999999\n")  # dead pid ignored
    assert P.find_dtach_master_pid(sock) is None


def test_sidecar_live_pid_without_sock_in_argv_ignored(tmp_path):
    sock = str(tmp_path / "z.sock")
    (tmp_path / "z.sock.pid").write_text(f"{os.getpid()}\n")  # live, unrelated argv
    assert P._read_pid_sidecar(sock) is None
    assert P.find_dtach_master_pid(sock) != os.getpid()


def test_ps_fallback_without_proc(tmp_path, monkeypatch):
    sock = str(tmp_path / "y.sock")
    monkeypatch.setattr(os.path, "isdir", lambda p: False if p == "/proc" else os.path.exists(p))
    ps_out = (
        f"  100     1 dtach -n {sock} -z sh\n"
        f"  101   100 sh\n"
        f"  200     1 dtach -a {sock}\n"
    )

    class R:
        stdout = ps_out
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert P.find_dtach_master_pid(sock) == 100


def test_python_dtach_end_to_end(py_dtach):
    name = P.spawn("sh", name="pyd")
    sp = P.sock_path(name)
    pidf = sp + ".pid"
    assert os.path.exists(pidf)
    pid = int(Path(pidf).read_text())
    P.send(name, "echo hi$((1+2))")
    assert _wait(lambda: "hi3" in P.read(name, settle=0.3))
    P.kill(name)
    assert _wait(lambda: not P._pid_alive(pid))
    assert not os.path.exists(sp) and not os.path.exists(pidf)


def test_daemon_exits_on_sock_unlink(py_dtach):
    name = P.spawn("sh", name="pyd2")
    sp = P.sock_path(name)
    pid = int(Path(sp + ".pid").read_text())
    os.unlink(sp)
    assert _wait(lambda: not P._pid_alive(pid), 5)


def test_daemon_does_not_delete_replacement_session_files(py_dtach):
    name = P.spawn("sh", name="pyd4")
    sp = P.sock_path(name)
    pidf = sp + ".pid"
    pid = int(Path(pidf).read_text())
    os.unlink(sp)
    Path(sp).write_text("")  # "new session" socket stand-in at same path
    Path(pidf).write_text(f"{os.getpid()}\n")  # new session's pidfile
    assert _wait(lambda: not P._pid_alive(pid), 5)
    assert os.path.exists(sp) and Path(pidf).read_text().strip() == str(os.getpid())


def test_inject_or_send_returns_tail(py_dtach):
    name = P.spawn("sh", name="pyd3")
    try:
        out = P.inject_or_send(name, "echo zz$((2+3))", settle_s=0.5)
        assert "zz5" in out
    finally:
        P.kill(name)
