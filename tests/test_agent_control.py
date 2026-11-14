"""Volatile loop control and status over the control socket: <ws>/run/agent-NAME.ctl."""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from nexus_lite import _db
from nexus_lite import agent_run as ar
from nexus_lite.agent_run import AgentLoop
from nexus_lite.memory import mtm

PY = sys.executable
ECHO = [PY, "-c", "import sys; sys.stdout.write('got:' + sys.stdin.read())"]


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = Path(tempfile.mkdtemp(prefix="nl", dir="/tmp"))  # short: Unix socket paths are limited
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(w / "s"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _db.reset_init()
    yield w
    shutil.rmtree(w, ignore_errors=True)


@pytest.fixture
def loops():
    made = []
    yield made
    for lp in made:
        lp.stop()


def make(loops, cmd=ECHO, name="worker", **kw):
    kw.setdefault("poll_s", 30)  # only wake makes it look again quickly
    lp = AgentLoop(name, cmd, **kw)
    loops.append(lp)
    return lp


def test_control_socket_path_and_mode(loops, ws):
    lp = make(loops).start()
    p = ws / "run" / "agent-worker.ctl"
    assert ar.control_path("worker").resolve() == p.resolve()
    assert p.exists()
    assert (os.stat(ws / "run").st_mode & 0o777) == 0o700
    lp.stop()
    assert not p.exists()


def test_status_over_socket(loops):
    lp = make(loops).start()
    assert lp.wait(until="idle", timeout=5)
    st = ar.control("worker", "status")
    assert st["status"] == "idle"


def test_no_listener_is_an_immediate_error_and_nothing_is_kept(ws):
    t0 = time.monotonic()
    with pytest.raises(ar.ControlUnavailable):
        ar.control("nobody", "wake")
    assert time.monotonic() - t0 < 1
    assert not (ws / "run" / "agent-nobody.ctl").exists()


def test_wake_over_socket(loops):
    lp = make(loops).start()
    assert lp.wait(until="idle", timeout=5)
    mtm.bus_send("worker", "hi", sender="alice")
    ar.control("worker", "wake")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not mtm.bus_read(target="alice", limit=10):
        time.sleep(0.02)
    assert [m["content"] for m in mtm.bus_read(target="alice", limit=10)] == ["got:hi"]


def test_stop_over_socket(loops):
    lp = make(loops).start()
    assert lp.wait(until="idle", timeout=5)
    ar.control("worker", "stop")
    assert lp.wait(until="stopped", timeout=10)


def test_context_set_and_reset_over_socket(loops, tmp_path):
    lp = make(loops).start()
    ar.control("worker", "context-set", path="/x/y.ctx")
    assert lp.context() == "/x/y.ctx"
    ar.control("worker", "context-reset")
    assert lp.context() is None


def test_unknown_op_rejected(loops):
    make(loops).start()
    with pytest.raises(ValueError):
        ar.control("worker", "explode")


def test_second_loop_with_same_name_refused_at_start(loops):
    make(loops).start()
    with pytest.raises(ar.AgentBusy):
        make(loops).start()
    assert ar.control("worker", "status")["status"] in ("idle", "busy", "starting")


def test_status_is_lifecycle_only_busy_carries_seq(loops):
    lp = make(loops, cmd=[PY, "-c", "import sys, time; sys.stdin.read(); time.sleep(0.5)"]).start()
    mtm.bus_send("worker", "x", sender="alice")
    lp.wake()
    assert lp.wait(until="busy", timeout=5)
    st = ar.control("worker", "status")
    assert st["status"] == "busy" and isinstance(st["seq"], int)
    assert lp.wait(until="idle", timeout=10)
    st = ar.control("worker", "status")
    assert st == {"ok": True, "status": "idle", "seq": None}  # no results or history in status


def test_stale_socket_file_is_replaced(loops, ws):
    (ws / "run").mkdir(mode=0o700, exist_ok=True)
    (ws / "run" / "agent-worker.ctl").write_text("stale")
    make(loops).start()
    assert ar.control("worker", "status")["status"] in ("starting", "idle")


# --- CLI: the same status over the control socket ----------------------------------------

import subprocess  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def cli_env(ws, tmp_path):
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(ROOT),
            "NEXUS_LITE_DIR": str(ws), "NEXUS_LITE_SOCK_DIR": str(ws / "s")}


def cli(env, *args, timeout=30):
    return subprocess.run([PY, "-m", "nexus_lite", "agent", *args], env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_cli_status_wait_ctl_against_a_running_cli_loop(ws, tmp_path):
    env = cli_env(ws, tmp_path)
    runner = subprocess.Popen([PY, "-m", "nexus_lite", "agent", "run", "worker", "--poll", "30", "--", *ECHO],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10  # no listener yet is an error by design: wait for the runner to bind
        while time.monotonic() < deadline and not (ws / "run" / "agent-worker.ctl").exists():
            time.sleep(0.05)
        p = cli(env, "wait", "worker", "--until", "idle", "--timeout", "10")
        assert p.returncode == 0, p.stderr
        p = cli(env, "status", "worker")
        assert (p.returncode, p.stdout.strip()) == (0, "idle")
        mtm.bus_send("worker", "hi", sender="alice")
        assert cli(env, "ctl", "worker", "wake").returncode == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not mtm.bus_read(target="alice", limit=10):
            time.sleep(0.05)
        assert [m["content"] for m in mtm.bus_read(target="alice", limit=10)] == ["got:hi"]
        assert cli(env, "ctl", "worker", "context", "set", "/x/y.ctx").returncode == 0
        assert cli(env, "ctl", "worker", "context", "reset").returncode == 0
        assert cli(env, "ctl", "worker", "stop").returncode == 0
        runner.communicate(timeout=15)  # also closes the runner's pipes
        assert runner.returncode == 0
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.communicate()
    p = cli(env, "status", "worker")
    assert p.returncode == 3 and "not running" in p.stderr
    assert not (ws / "run" / "agent-worker.ctl").exists()


def test_cli_wait_timeout_exit_1(loops, ws, tmp_path):
    lp = make(loops, cmd=[PY, "-c", "import sys, time; sys.stdin.read(); time.sleep(10)"]).start()
    mtm.bus_send("worker", "x", sender="alice")
    lp.wake()
    assert lp.wait(until="busy", timeout=5)
    p = cli(cli_env(ws, tmp_path), "wait", "worker", "--until", "idle", "--timeout", "0.5")
    assert p.returncode == 1
    p = cli(cli_env(ws, tmp_path), "status", "worker")
    assert p.stdout.strip().startswith("busy seq=")


def test_cli_ctl_no_listener_exit_3(ws, tmp_path):
    p = cli(cli_env(ws, tmp_path), "ctl", "nobody", "wake")
    assert p.returncode == 3 and "not running" in p.stderr


# --- long workspace paths: workspace-scoped fallback (same rule as the PTY socket dir) -----------------

def test_long_workspace_uses_a_hashed_tmp_dir_and_the_loop_still_works(loops, tmp_path, monkeypatch):
    import hashlib
    long_ws = (tmp_path / ("d" * 80)).resolve()
    long_ws.mkdir()
    monkeypatch.setenv("NEXUS_LITE_DIR", str(long_ws))
    monkeypatch.setenv("NEXUS_LITE_HOME", str(long_ws))
    _db.reset_init()
    want = Path("/tmp") / ("nx-" + hashlib.sha256(str(long_ws).encode()).hexdigest()[:12])
    try:
        assert ar.control_path("stats_helper") == want / "agent-stats_helper.ctl"
        lp = make(loops, name="stats_helper").start()
        assert lp.wait(until="idle", timeout=5)
        assert ar.control("stats_helper", "status")["status"] == "idle"
        assert (os.stat(want).st_mode & 0o777) == 0o700
    finally:
        for lp in loops:
            lp.stop()
        shutil.rmtree(want, ignore_errors=True)


def test_a_planted_fallback_link_is_refused(tmp_path, monkeypatch):
    import hashlib
    long_ws = (tmp_path / ("e" * 80)).resolve()
    long_ws.mkdir()
    monkeypatch.setenv("NEXUS_LITE_DIR", str(long_ws))
    _db.reset_init()
    want = Path("/tmp") / ("nx-" + hashlib.sha256(str(long_ws).encode()).hexdigest()[:12])
    os.symlink(str(tmp_path), str(want))
    try:
        with pytest.raises(RuntimeError):
            AgentLoop("worker", ECHO, poll_s=30).start()
    finally:
        os.unlink(str(want))
