"""Acceptance #8: a PTY member and an `agent run` member talk on the same bus (no PTY code changed).

lead: a long-lived process in a dtach PTY; each injected pointer line wakes it to read its own inbox and it replies
with the bus itself. calc: `agent run` (one command per message). alice -> lead -> calc -> lead -> alice.
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

from nexus_lite import _db, pty
from nexus_lite.agent_run import AgentLoop
from nexus_lite.memory import mtm

PY = sys.executable
ROOT = Path(__file__).resolve().parent.parent

LEAD = """
import sys
from nexus_lite.memory import mtm
for _line in sys.stdin:  # each injected pointer line is a wake
    for m in mtm.bus_read(target="lead", unacked=True, consumer="lead"):
        if m["target"] == "lead" and m["sender"] == "alice":
            mtm.bus_send("calc", m["content"], sender="lead")
        elif m["target"] == "lead" and m["sender"] == "calc":
            mtm.bus_send("alice", "done:" + m["content"], sender="lead")
        mtm.bus_ack("lead", m["seq"], consumer="lead")
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX PTY")


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = Path(tempfile.mkdtemp(prefix="nl", dir="/tmp"))
    for k, v in {"NEXUS_LITE_HOME": w, "NEXUS_LITE_DIR": w, "NEXUS_LITE_SOCK_DIR": w / "s",
                 "HOME": tmp_path / "home", "SHELL": "/bin/sh", "PYTHONPATH": ROOT}.items():
        monkeypatch.setenv(k, str(v))
    _db.reset_init()
    yield w
    shutil.rmtree(w, ignore_errors=True)


def test_pty_member_and_command_member_on_one_bus(ws):
    script = ws / "lead.py"
    script.write_text(LEAD)
    pty.spawn([PY, "-u", str(script)], name="lead")
    calc = AgentLoop("calc", [PY, "-c", "import sys; sys.stdout.write('got:' + sys.stdin.read())"], poll_s=0.05)
    calc.start()
    try:
        time.sleep(0.5)  # let the lead process reach its read loop
        mtm.bus_send("lead", "2+2", sender="alice")
        deadline = time.monotonic() + 20
        got = []
        while time.monotonic() < deadline and not got:
            got = [m["content"] for m in mtm.bus_read(target="alice", limit=10)]
            time.sleep(0.05)
        assert got == ["done:got:2+2"]
    finally:
        calc.stop()
        pty.kill("lead")
