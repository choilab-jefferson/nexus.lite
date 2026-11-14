"""AgentLoop (library): status is the state; on_status_change is a same-process notification of a change."""

import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from nexus_lite import _db
from nexus_lite.agent_run import AgentLoop
from nexus_lite.memory import mtm

PY = sys.executable
ECHO = [PY, "-c", "import sys; sys.stdout.write('got:' + sys.stdin.read())"]


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = Path(tempfile.mkdtemp(prefix="nl", dir="/tmp"))  # short: the loop binds <ws>/run/agent-NAME.ctl
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


def make(loops, cmd=ECHO, **kw):
    kw.setdefault("poll_s", 0.02)
    lp = AgentLoop("worker", cmd, **kw)
    loops.append(lp)
    return lp


def test_status_before_start_and_after_stop(loops):
    lp = make(loops)
    assert lp.status() == "stopped"
    lp.start()
    assert lp.wait(until="idle", timeout=5)
    lp.stop()
    assert lp.status() == "stopped"


def test_status_changes_notified_once_per_change(loops):
    seen = []
    mtm.bus_send("worker", "hi", sender="alice")
    lp = make(loops)
    lp.on_status_change(lambda old, new: seen.append((old, new)))
    lp.start()
    assert lp.wait(until="idle", timeout=5)
    time.sleep(0.2)  # several idle polls: no repeated notification
    lp.stop()
    assert seen == [("stopped", "starting"), ("starting", "busy"), ("busy", "idle"), ("idle", "stopped")]
    assert [m["content"] for m in mtm.bus_read(target="alice", limit=10)] == ["got:hi"]


def test_wait_times_out_when_status_not_reached(loops):
    lp = make(loops, cmd=[PY, "-c", "import time; time.sleep(5)"])
    mtm.bus_send("worker", "x", sender="alice")
    lp.start()
    assert lp.wait(until="busy", timeout=5)
    t0 = time.monotonic()
    assert lp.wait(until="idle", timeout=0.3) is False
    assert time.monotonic() - t0 < 2


def test_stop_while_busy_ends_stopped(loops):
    lp = make(loops, cmd=[PY, "-c", "import time; time.sleep(30)"])
    mtm.bus_send("worker", "x", sender="alice")
    lp.start()
    assert lp.wait(until="busy", timeout=5)
    t0 = time.monotonic()
    lp.stop()
    assert lp.status() == "stopped"
    assert time.monotonic() - t0 < 10


def test_wake_skips_the_idle_poll_interval(loops):
    lp = make(loops, poll_s=30)
    lp.start()
    assert lp.wait(until="idle", timeout=5)
    mtm.bus_send("worker", "hi", sender="alice")
    lp.wake()
    assert lp.wait(until="busy", timeout=3) or mtm.bus_read(target="alice", limit=10)
    assert lp.wait(until="idle", timeout=5)
    assert [m["content"] for m in mtm.bus_read(target="alice", limit=10)] == ["got:hi"]


def test_callback_error_does_not_stop_the_loop(loops):
    mtm.bus_send("worker", "hi", sender="alice")
    lp = make(loops)
    lp.on_status_change(lambda old, new: 1 / 0)
    lp.start()
    assert lp.wait(until="idle", timeout=5)
    assert [m["content"] for m in mtm.bus_read(target="alice", limit=10)] == ["got:hi"]


def test_unknown_wait_status_rejected(loops):
    lp = make(loops)
    with pytest.raises(ValueError):
        lp.wait(until="done", timeout=0.1)


def test_invoke_runs_once_without_a_loop():
    from nexus_lite.agent_run import invoke
    r = invoke(ECHO, "hi")
    assert r.ok and r.stdout == "got:hi"


# --- external context between invocations ------------------------------------------

COUNTER = (
    "import sys, os, time\n"
    "src, dst, gate = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "n = int(open(src).read()) if src else 0\n"
    "body = sys.stdin.read()\n"
    "while body == 'slow' and not os.path.exists(gate):\n"
    "    time.sleep(0.02)\n"
    "open(dst, 'w').write(str(n + 1))\n"
    "print(n, end='')\n"
)


def counter_loop(loops, tmp_path):
    p = tmp_path / "counter.py"
    p.write_text(COUNTER)
    return make(loops, cmd=[PY, str(p), "{context_in}", "{context_out}", str(tmp_path / "gate")])


def ask(lp, body):
    mtm.bus_send("worker", body, sender="alice")
    lp.wake()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        got = sorted(mtm.bus_read(target="alice", limit=100), key=lambda m: m["seq"])
        if got and got[-1]["content"] is not None and len(got) > ask.n:
            ask.n = len(got)
            return got[-1]["content"]
        time.sleep(0.02)
    raise AssertionError("no reply")


@pytest.fixture(autouse=True)
def _reset_ask():
    ask.n = 0


def test_set_and_reset_context_between_calls(loops, tmp_path):
    lp = counter_loop(loops, tmp_path).start()
    assert ask(lp, "a") == "0"
    assert ask(lp, "b") == "1"
    ext = tmp_path / "ext.ctx"
    ext.write_text("50")
    lp.set_context(str(ext))
    assert ask(lp, "c") == "50"
    assert ask(lp, "d") == "51"
    lp.reset_context()
    assert ask(lp, "e") == "0"
    assert ext.read_text() == "50"


def test_external_set_during_a_call_wins_over_its_output(loops, tmp_path):
    lp = counter_loop(loops, tmp_path).start()
    assert ask(lp, "a") == "0"
    assert lp.wait(until="idle", timeout=5)  # the previous call is fully done before the slow one starts
    mtm.bus_send("worker", "slow", sender="alice")
    lp.wake()
    assert lp.wait(until="busy", timeout=5)
    ext = tmp_path / "ext.ctx"
    ext.write_text("100")
    lp.set_context(str(ext))
    (tmp_path / "gate").write_text("go")
    assert lp.wait(until="idle", timeout=10)
    ask.n += 1  # the slow call's reply ("1")
    assert ask(lp, "c") == "100"
