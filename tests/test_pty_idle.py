"""PTY idle detection: wait() must not return before the agent answered."""

import os
import shutil
import tempfile
import textwrap
import time

import pytest

from nexus_lite import pty

pytestmark = pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach required")

FAKE_AGENT = textwrap.dedent(
    """
    import sys, time, signal
    state = ["accept-edits > "]
    def redraw(*a):
        sys.stdout.write("\\r\x1b[2K" + state[0]); sys.stdout.flush()
    signal.signal(signal.SIGWINCH, redraw)
    delay, busy = float(sys.argv[1]), float(sys.argv[2])
    sys.stdout.write("accept-edits > \\n"); sys.stdout.flush()
    sys.stdin.readline()
    state[0] = "accept-edits > go"
    time.sleep(delay)  # silent start
    end = time.time() + busy
    glyphs = "⣷⣾⣽⣻⢿⡿⣟⣯"
    i = 0
    while time.time() < end:
        state[0] = "%s Generating... esc to cancel" % glyphs[i % 8]
        sys.stdout.write("\\r" + state[0]); sys.stdout.flush()
        i += 1
        time.sleep(0.1)
    state[0] = "PONG\\naccept-edits > "
    sys.stdout.write("\\r\x1b[2K" + state[0] + "\\n"); sys.stdout.flush()
    time.sleep(60)
    """
)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    d = tempfile.mkdtemp(prefix="nlp")
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", d)
    yield d
    for s in pty.list_sessions():
        pty.kill(s["name"])
    shutil.rmtree(d, ignore_errors=True)


def _agent(env, delay, busy):
    script = os.path.join(env, "fake.py")
    with open(script, "w") as f:
        f.write(FAKE_AGENT)
    import sys
    pty.spawn(f"{sys.executable} {script} {delay} {busy}", name="t")
    return pty.wait("t", provider="agy", timeout=10)


def test_wait_after_send_does_not_return_before_answer(env):
    _agent(env, 0, 4)
    pty.send("t", "go")
    t0 = time.monotonic()
    screen = pty.wait("t", provider="agy", timeout=20, settle=1.0)
    assert "PONG" in screen
    assert time.monotonic() - t0 >= 3.0


def test_slow_start_before_spinner(env):
    _agent(env, 1.5, 2)
    pty.send("t", "go")
    screen = pty.wait("t", provider="agy", timeout=20, settle=3.0)
    assert "PONG" in screen


def test_never_finishes_times_out(env):
    _agent(env, 0, 120)
    pty.send("t", "go")
    with pytest.raises(TimeoutError):
        pty.wait("t", provider="agy", timeout=4, settle=1.0)


def test_bash_still_idle_quickly(env):
    pty.spawn("bash --norc", name="t")
    pty.wait("t", provider="bash", timeout=5)
    pty.send("t", "echo HELLO_QUICK")
    t0 = time.monotonic()
    screen = pty.wait("t", provider="bash", timeout=10)
    assert "HELLO_QUICK" in screen
    assert time.monotonic() - t0 < 2.0


def test_busy_markers_agy():
    assert pty.is_busy("Generating...", "agy")
    assert pty.is_busy("⣷ x", "agy")
    assert pty.is_busy("foo esc to cancel", "agy")


def test_trust_prompt_not_answered_by_default(env, monkeypatch):
    monkeypatch.delenv("NEXUS_LITE_PTY_AUTO_TRUST", raising=False)
    calls = []
    monkeypatch.setattr(pty, "keys", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(pty, "read", lambda *a, **k: "Do you trust the contents ⣷")
    with pytest.raises(TimeoutError):
        pty.wait("x", provider="agy", timeout=1, poll=0.2)
    assert calls == []


def test_read_renders_at_capture_geometry(env):
    pty.spawn("bash --norc", name="t")
    pty.wait("t", provider="bash", timeout=5)
    pty.send("t", "printf '%0200d\\n' 7")
    time.sleep(0.5)
    assert "0" * 190 in pty.read("t")
