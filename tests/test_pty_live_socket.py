"""A live master's socket is never replaced, and a caller's own ``<sock>.lock`` never deadlocks create.

Regression tests for the 2026-11-16 host incident: viewer terminals failed with
``lock timeout for SOCK`` then ``SOCK: No such file or directory`` once the lite server became the default backend.
"""

import fcntl
import os
import shutil
import signal
import socket
import subprocess
import time

import pytest

from nexus_lite.pty import protocol, server

from .test_ptyd import Client, env, launch  # noqa: F401

pytestmark = pytest.mark.skipif(not hasattr(os, "forkpty"), reason="POSIX pty required")


def test_create_succeeds_while_a_launcher_holds_the_dtach_era_lock(env, tmp_path):
    """nexus.code launchers hold ``<sock>.lock`` around the spawn (host_launch ``flock 9``, create_session)."""
    sp = str(tmp_path / "held.sock")
    with open(f"{sp}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        t0 = time.monotonic()
        p = launch(env, sp)
        elapsed = time.monotonic() - t0
    assert p.returncode == 0, p.stderr
    assert elapsed < 5.0
    assert Client(sp).ready()


def _busy_listener(sp):
    """A listening socket whose master never accepts and whose backlog is full: connect probes time out."""
    lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lst.bind(sp)
    lst.listen(0)
    pending = []
    for _ in range(64):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.setblocking(False)
        try:
            c.connect(sp)
        except (BlockingIOError, InterruptedError):
            c.close()
            break
        pending.append(c)
    return lst, pending


def test_bind_keeps_a_busy_masters_socket(tmp_path):
    sp = str(tmp_path / "busy.sock")
    lst, pending = _busy_listener(sp)
    try:
        assert not protocol.probe_connect(sp, timeout=0.2)  # the incident's precondition
        ident = protocol.file_ident(sp)
        assert server._bind(sp, exist_ok=True) is None
        with pytest.raises(server.SessionExists):
            server._bind(sp, exist_ok=False)
        assert protocol.file_ident(sp) == ident
    finally:
        for c in pending:
            c.close()
        lst.close()


def test_bind_keeps_a_listening_socket_when_the_probe_fails(tmp_path, monkeypatch):
    sp = str(tmp_path / "slow.sock")
    lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lst.bind(sp)
    lst.listen(1)
    monkeypatch.setattr(server, "probe_connect", lambda *a, **k: False)
    try:
        ident = protocol.file_ident(sp)
        assert server._bind(sp, exist_ok=True) is None
        assert protocol.file_ident(sp) == ident
    finally:
        lst.close()


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not installed")
def test_bind_keeps_a_dtach_masters_socket(tmp_path, monkeypatch):
    sp = str(tmp_path / "dt.sock")
    subprocess.run(["dtach", "-n", sp, "sleep", "300"], check=True, timeout=10)
    deadline = time.monotonic() + 5
    while not os.path.exists(sp) and time.monotonic() < deadline:
        time.sleep(0.05)
    monkeypatch.setattr(server, "probe_connect", lambda *a, **k: False)
    try:
        ident = protocol.file_ident(sp)
        assert server._bind(sp, exist_ok=True) is None
        assert protocol.file_ident(sp) == ident
    finally:
        out = subprocess.run(["pgrep", "-f", f"dtach -n {sp}"], capture_output=True, text=True).stdout.split()
        for pid in out:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass


def test_bind_still_replaces_a_socket_nobody_listens_on(tmp_path):
    sp = str(tmp_path / "stale.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sp)
    s.close()  # file left behind, no listener
    lst = server._bind(sp, exist_ok=True)
    assert lst is not None
    lst.close()
    os.unlink(sp)


def _stub(path, calls):
    st = server._Server.__new__(server._Server)
    st.path = path
    st._input = lambda data: calls.append(("input", data))
    return st


def test_push_forwards_input_before_stamping_and_throttles_the_stamp(tmp_path, monkeypatch):
    calls = []
    st = _stub(str(tmp_path / "t.sock"), calls)
    real_stamp = server._Server._stamp_input
    st._stamp_input = lambda: (calls.append(("stamp",)), real_stamp(st))
    replaced = []
    real_replace = os.replace
    monkeypatch.setattr(server.os, "replace", lambda a, b: (replaced.append(b), real_replace(a, b)))
    c = server._Client(None)
    c.attached = True
    keys = [bytes([0x61 + i % 26]) for i in range(200)]
    for k in keys:
        server._Server._handle(st, c, protocol.MSG_PUSH, 1, k)
    assert calls[0] == ("input", keys[0])  # the keystroke goes out before any stamp work
    assert [x[1] for x in calls if x[0] == "input"] == keys  # order unchanged
    assert 1 <= len(replaced) <= 2  # a 200-key burst writes the stamp once, not 200 times
    with open(f"{st.path}.input") as f:
        assert float(f.read()) > 0
