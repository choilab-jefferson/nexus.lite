"""`agent run` restart record and context reference placeholders ()."""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from nexus_lite import _db
from nexus_lite import agent_run as ar
from nexus_lite.memory import mtm

PY = sys.executable

# argv[1] = context_in ('' = none), argv[2] = context_out; prints the counter it read, writes counter+1
COUNTER = (
    "import sys\n"
    "src, dst = sys.argv[1], sys.argv[2]\n"
    "n = int(open(src).read()) if src else 0\n"
    "body = sys.stdin.read()\n"
    "open(dst, 'w').write(str(n + 1))\n"
    "print(n, end='')\n"
)


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = Path(tempfile.mkdtemp(prefix="nl", dir="/tmp"))  # short: the CLI loop binds <ws>/run/agent-NAME.ctl
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(w / "s"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _db.reset_init()
    yield w
    shutil.rmtree(w, ignore_errors=True)


def counter_cmd(tmp_path, extra=""):
    p = tmp_path / "counter.py"
    p.write_text(COUNTER + extra)
    return [PY, str(p), "{context_in}", "{context_out}"]


def send(n):
    for i in range(n):
        mtm.bus_send("worker", f"m{i}", sender="alice")


def reply_texts():
    msgs = sorted(mtm.bus_read(target="alice", limit=100), key=lambda m: m["seq"])
    return [m["content"] for m in msgs]


def run(cmd, **kw):
    kw.setdefault("exit_when_idle", True)
    kw.setdefault("poll_s", 0.01)
    return ar.agent_run("worker", cmd, **kw)


# --- #12 restart -------------------------------------------------------------------------


def test_restart_does_not_rerun_acked_messages(tmp_path):
    log = tmp_path / "calls.txt"
    p = tmp_path / "count.py"
    p.write_text(f"import sys\nopen({str(log)!r}, 'a').write(sys.stdin.read() + '\\n')\n")
    send(2)
    assert run([PY, str(p)], max_messages=1) == 1
    assert run([PY, str(p)]) == 1  # restart: only the unacked message runs
    assert log.read_text().split() == ["m0", "m1"]


# --- placeholder expansion ---------------------------------------------------------------


def test_expand_command_argv_and_shell():
    argv = ["loom", "--context-in", "{context_in}", "--context-out={context_out}", "x"]
    assert ar.expand_command(argv, "/a b", "/c") == ["loom", "--context-in", "/a b", "--context-out=/c", "x"]
    assert ar.expand_command("run {context_in} {context_out}", "", "/c") == "run '' /c"
    assert ar.expand_command("run {context_in}", "/a b", "/c") == "run '/a b'"


def test_command_without_placeholders_is_unchanged():
    assert ar.expand_command(["echo", "hi"], "/a", "/b") == ["echo", "hi"]


# --- context reference between invocations -----------------------------------------------


def test_first_call_has_no_context_then_success_promotes(tmp_path):
    send(3)
    run(counter_cmd(tmp_path))
    assert reply_texts() == ["0", "1", "2"]


def test_initial_context_reference_is_used(tmp_path):
    start = tmp_path / "start.ctx"
    start.write_text("10")
    send(2)
    run(counter_cmd(tmp_path), context=str(start))
    assert reply_texts() == ["10", "11"]
    assert start.read_text() == "10"  # an external reference is never modified or removed


def test_failed_call_does_not_promote_and_leaves_no_out_file(tmp_path, ws):
    # message m1 fails after writing its context_out
    cmd = counter_cmd(tmp_path, extra="if body == 'm1':\n    sys.exit(3)\n")
    send(3)
    run(cmd, on_failure="silent")
    assert reply_texts() == ["0", "1"]  # 3rd call read the context promoted by the 1st call
    ctx_files = list((ws / "agents" / "worker" / "context").iterdir())
    assert len(ctx_files) == 1  # only the current reference; failed and superseded outputs removed


def test_success_without_out_file_keeps_previous_reference(tmp_path):
    start = tmp_path / "start.ctx"
    start.write_text("5")
    p = tmp_path / "reader.py"
    p.write_text("import sys\nsys.stdin.read()\nprint(open(sys.argv[1]).read(), end='')\n")
    send(2)
    run([PY, str(p), "{context_in}", "{context_out}"], context=str(start))
    assert reply_texts() == ["5", "5"]


def test_cli_context_start_argument(tmp_path, ws):
    import subprocess
    root = Path(__file__).resolve().parent.parent
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(root),
           "NEXUS_LITE_DIR": str(ws), "NEXUS_LITE_SOCK_DIR": str(tmp_path / "socks")}
    start = tmp_path / "start.ctx"
    start.write_text("7")
    send(1)
    p = subprocess.run([PY, "-m", "nexus_lite", "agent", "run", "worker", "--exit-when-idle", "--poll", "0.05",
                        "--context", str(start), "--", *counter_cmd(tmp_path)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    _db.reset_init()
    assert reply_texts() == ["7"]
