"""pty.kill ends the session's whole process group, not only the dtach master.

spawn records the PTY leader's pgid in <sock>.pgid; kill sends SIGTERM then SIGKILL to that group (even when the master
is already gone) and kill_session returns any pid of the group that still survives.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from nexus_lite import _db, pty


@pytest.fixture(params=["dtach", "python"])
def ws(request, tmp_path, monkeypatch):
    if request.param == "dtach" and not shutil.which("dtach"):
        pytest.skip("dtach binary not installed")
    w = Path(tempfile.mkdtemp(prefix="nl", dir="/tmp")).resolve()
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(w / "s"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    if request.param == "dtach":
        monkeypatch.setenv("NEXUS_LITE_DTACH", "binary")  # opt-in oracle: the real dtach binary
    else:
        monkeypatch.delenv("NEXUS_LITE_DTACH", raising=False)  # default: the stdlib server
    _db.reset_init()
    yield w
    shutil.rmtree(w, ignore_errors=True)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def spawn_with_hup_immune_child(ws: Path) -> int:
    pidfile = ws / "gc.pid"
    pty.spawn(f"nohup sleep 300 >/dev/null 2>&1 & echo $! > {pidfile}; exec cat", name="w")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.05)
    return int(pidfile.read_text())


def wait_dead(pid: int, s: float = 5.0) -> bool:
    end = time.monotonic() + s
    while time.monotonic() < end and alive(pid):
        time.sleep(0.05)
    return not alive(pid)


def test_spawn_records_leader_pgid(ws):
    gc = spawn_with_hup_immune_child(ws)
    try:
        sp = pty.sock_path("w")
        pgid = int(Path(f"{sp}.pgid").read_text())
        assert os.getpgid(gc) == pgid
    finally:
        pty.kill("w")
        if alive(gc):
            os.kill(gc, signal.SIGKILL)


def test_kill_ends_hup_immune_descendants(ws):
    gc = spawn_with_hup_immune_child(ws)
    try:
        assert pty.kill("w") is True
        assert wait_dead(gc), "nohup child survived pty.kill"
        assert not Path(pty.sock_path("w") + ".pgid").exists()
    finally:
        if alive(gc):
            os.kill(gc, signal.SIGKILL)


def test_kill_after_master_died_still_ends_the_group(ws):
    gc = spawn_with_hup_immune_child(ws)
    try:
        master = pty.find_dtach_master_pid(pty.sock_path("w"))
        os.kill(master, signal.SIGKILL)
        time.sleep(0.3)
        assert alive(gc)  # the leftover the old kill could not see
        assert pty.kill_session("w") == []
        assert wait_dead(gc)
    finally:
        if alive(gc):
            os.kill(gc, signal.SIGKILL)


def test_kill_session_never_signals_our_own_group(ws, monkeypatch):
    gc = spawn_with_hup_immune_child(ws)
    try:
        Path(pty.sock_path("w") + ".pgid").write_text(str(os.getpgid(0)))  # a corrupted sidecar
        assert pty.kill_session("w") == []  # still alive: we are not killed and nothing else is touched
    finally:
        if alive(gc):
            os.kill(gc, signal.SIGKILL)
