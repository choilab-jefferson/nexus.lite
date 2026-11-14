"""`nexus-lite agent run`: one command per bus message (fake commands only, no loom)."""

import os
import signal
import subprocess
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from nexus_lite import _db
from nexus_lite import agent_run as ar
from nexus_lite.memory import mtm

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


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


def script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o755)
    return p


def py(tmp_path, body, name="agent.py"):
    return [PY, str(script(tmp_path, name, "import os, sys, time\n" + body))]


ECHO = "data = sys.stdin.read()\nsys.stderr.write('LOGLINE\\n')\nsys.stdout.write('got:' + data)\n"


def replies(to="alice"):
    return [m for m in mtm.bus_read(target=to, limit=100)]


def unacked(name="worker"):
    return mtm.bus_read(target=name, unacked=True, consumer=ar.CONSUMER)


def run_once(cmd, **kw):
    kw.setdefault("exit_when_idle", True)
    kw.setdefault("poll_s", 0.01)
    return ar.agent_run("worker", cmd, **kw)


# --- #4 idle ----------------------------------------------------------------------------


def test_idle_does_not_run_command(tmp_path):
    marker = tmp_path / "ran"
    cmd = py(tmp_path, f"open({str(marker)!r}, 'w').write('x')\n")
    assert run_once(cmd) == 0
    assert not marker.exists()


def test_idle_waits_then_handles_late_message(tmp_path):
    cmd = py(tmp_path, ECHO)
    t = threading.Timer(0.2, lambda: mtm.bus_send("worker", "late", sender="alice"))
    t.start()
    n = ar.agent_run("worker", cmd, poll_s=0.05, max_messages=1)
    t.join()
    assert n == 1
    assert [m["content"] for m in replies()] == ["got:late"]


# --- #5 one message ---------------------------------------------------------------------


def test_one_message_reply_to_sender_stderr_not_published(tmp_path, capfd):
    mtm.bus_send("worker", "hello", sender="alice")
    assert run_once(py(tmp_path, ECHO)) == 1
    got = replies()
    assert [(m["sender"], m["content"]) for m in got] == [("worker", "got:hello")]
    assert all("LOGLINE" not in (m["content"] or "") for m in mtm.bus_read(limit=100))
    assert "LOGLINE" in capfd.readouterr().err
    assert unacked() == []


def test_utf8_body_on_stdin(tmp_path):
    mtm.bus_send("worker", "안녕 ✓", sender="alice")
    run_once(py(tmp_path, ECHO))
    assert replies()[0]["content"] == "got:안녕 ✓"


def test_empty_stdout_no_reply_but_acked(tmp_path):
    mtm.bus_send("worker", "x", sender="alice")
    assert run_once(py(tmp_path, "sys.stdin.read()\n")) == 1
    assert replies() == []
    assert unacked() == []


def test_fifo_one_run_per_message(tmp_path):
    log = tmp_path / "order"
    cmd = py(tmp_path, f"open({str(log)!r}, 'a').write(sys.stdin.read() + '\\n')\n")
    for b in ("a", "b", "c"):
        mtm.bus_send("worker", b, sender="alice")
    assert run_once(cmd) == 3
    assert log.read_text().split() == ["a", "b", "c"]


def test_max_messages_bounds_iterations(tmp_path):
    for b in ("a", "b"):
        mtm.bus_send("worker", b, sender="alice")
    assert run_once(py(tmp_path, ECHO), max_messages=1) == 1
    assert [m["content"] for m in unacked()] == ["b"]


def test_broadcast_and_self_messages_are_skipped(tmp_path):
    marker = tmp_path / "ran"
    mtm.bus_send("ALL", "everyone", sender="alice")
    mtm.bus_send("worker", "loop", sender="worker")
    assert run_once(py(tmp_path, f"open({str(marker)!r}, 'w').write('x')\n")) == 0
    assert not marker.exists()
    assert unacked() == []


def test_env_allowlist(tmp_path, monkeypatch, ws):
    monkeypatch.setenv("SECRET_TOKEN", "s3cret")
    monkeypatch.setenv("PASSED_VAR", "ok")
    mtm.bus_send("worker", "x", sender="alice")
    cmd = py(tmp_path, "sys.stdin.read(); e = os.environ\n"
             "print(e.get('NEXUS_AGENT'), e.get('NEXUS_LITE_DIR'), e.get('SECRET_TOKEN'), e.get('PASSED_VAR'))\n")
    run_once(cmd, pass_env=["PASSED_VAR"])
    assert replies()[0]["content"].split() == ["worker", str(ws.resolve()), "None", "ok"]


def test_explicit_env_replaces_the_process_env(tmp_path, monkeypatch, ws):
    # a consumer that builds each member's environment (e.g. a sandbox with its own HOME) passes it as is;
    # nothing from the runner's own environment leaks in; NEXUS_AGENT and NEXUS_LITE_DIR are still set
    monkeypatch.setenv("SECRET_TOKEN", "s3cret")
    mtm.bus_send("worker", "x", sender="alice")
    cmd = py(tmp_path, "sys.stdin.read(); e = os.environ\n"
             "print(e.get('HOME'), e.get('ONLY_MINE'), e.get('SECRET_TOKEN'), e.get('NEXUS_AGENT'))\n")
    run_once(cmd, env={"PATH": os.environ["PATH"], "HOME": "/sandbox/home", "ONLY_MINE": "1"})
    assert replies()[0]["content"].split() == ["/sandbox/home", "1", "None", "worker"]


def test_agent_loop_passes_an_explicit_env(tmp_path, ws):
    mtm.bus_send("worker", "x", sender="alice")
    cmd = py(tmp_path, "sys.stdin.read(); print(os.environ.get('ONLY_MINE'))\n")
    lp = ar.AgentLoop("worker", cmd, poll_s=0.02, exit_when_idle=True,
                      env={"PATH": os.environ["PATH"], "ONLY_MINE": "yes"})
    lp.start()
    assert lp.wait(until="stopped", timeout=10)
    assert replies()[0]["content"].strip() == "yes"


def test_cwd_runs_the_command_in_that_folder(tmp_path, ws):
    work = tmp_path / "project"
    work.mkdir()
    mtm.bus_send("worker", "x", sender="alice")
    run_once(py(tmp_path, "sys.stdin.read(); print(os.getcwd())\n"), cwd=str(work))
    assert replies()[0]["content"].strip() == str(work.resolve())


# --- #6 failure -------------------------------------------------------------------------

FAIL = "sys.stdin.read(); sys.stdout.write('partial'); sys.stderr.write('SECRET-ERR'); sys.exit(3)\n"


def test_failure_notify_sender_only_exit_code(tmp_path):
    mtm.bus_send("worker", "x", sender="alice")
    cmd = py(tmp_path, FAIL)
    assert run_once(cmd) == 1
    assert [m["content"] for m in replies()] == ["worker failed: exit=3"]
    assert unacked() == []


def test_failure_silent_sends_nothing(tmp_path):
    mtm.bus_send("worker", "x", sender="alice")
    assert run_once(py(tmp_path, FAIL), on_failure="silent") == 1
    assert replies() == []
    assert unacked() == []


def test_invalid_on_failure_rejected(tmp_path):
    with pytest.raises(ValueError):
        run_once(py(tmp_path, ECHO), on_failure="loud")


# --- #7 timeout / stop cleanup ---------------------------------------------------------

GRANDCHILD = (
    "import subprocess\n"
    "sys.stdin.read()\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(os.environ['PIDFILE'], 'w').write(f'{os.getpid()} {g.pid}')\n"
    "sys.stdout.write('never'); sys.stdout.flush()\n"
    "time.sleep(60)\n"
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); treat it as gone
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def wait_gone(*pids, within=5.0):
    end = time.monotonic() + within
    while time.monotonic() < end and any(alive(p) for p in pids):
        time.sleep(0.05)
    return not any(alive(p) for p in pids)


def test_timeout_kills_process_group(tmp_path, monkeypatch):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    mtm.bus_send("worker", "x", sender="alice")
    t0 = time.monotonic()
    assert run_once(py(tmp_path, GRANDCHILD), timeout=1.0, pass_env=["PIDFILE"]) == 1
    assert time.monotonic() - t0 < 10
    child, grand = map(int, pidfile.read_text().split())
    assert wait_gone(child, grand)
    assert [m["content"] for m in replies()] == ["worker failed: timeout"]
    assert unacked() == []


def test_stop_cancels_running_command_without_ack(tmp_path, monkeypatch):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    mtm.bus_send("worker", "x", sender="alice")
    stop = threading.Event()

    def fire():
        while not pidfile.exists() or not pidfile.read_text().strip():
            time.sleep(0.02)
        stop.set()

    threading.Thread(target=fire, daemon=True).start()
    assert ar.agent_run("worker", py(tmp_path, GRANDCHILD), stop=stop, poll_s=0.01, pass_env=["PIDFILE"]) == 0
    child, grand = map(int, pidfile.read_text().split())
    assert wait_gone(child, grand)
    assert replies() == []  # cancelled: nothing published, no notify
    assert len(unacked()) == 1  # not acked: may run again (no exactly-once)


def test_leftover_background_process_cleaned_after_success(tmp_path, monkeypatch):
    pidfile = tmp_path / "pids"
    monkeypatch.setenv("PIDFILE", str(pidfile))
    mtm.bus_send("worker", "x", sender="alice")
    body = (
        "import subprocess\nsys.stdin.read()\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "open(os.environ['PIDFILE'], 'w').write(str(g.pid))\nprint('done')\n"
    )
    assert run_once(py(tmp_path, body), pass_env=["PIDFILE"]) == 1
    assert wait_gone(int(pidfile.read_text()))
    assert [m["content"] for m in replies()] == ["done\n"]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_cli_signal_stops_runner_and_cleans_up(tmp_path, ws, sig):
    # SIGHUP: the runner lives in a PTY that is closed (harness end of epoch); the command group must not survive it
    pidfile = tmp_path / "pids"
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    mtm.bus_send("worker", "x", sender="alice")
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(REPO_ROOT),
        "NEXUS_LITE_DIR": str(ws), "NEXUS_LITE_SOCK_DIR": str(tmp_path / "socks"),
        "PIDFILE": str(pidfile), "TMPDIR": str(tmpdir),
    }
    p = subprocess.Popen(
        [PY, "-m", "nexus_lite", "agent", "run", "worker", "--env", "PIDFILE", "--poll", "0.05", "--",
         *py(tmp_path, GRANDCHILD)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    end = time.monotonic() + 20
    while time.monotonic() < end and not (pidfile.exists() and pidfile.read_text().strip()):
        time.sleep(0.05)
    child, grand = map(int, pidfile.read_text().split())
    p.send_signal(sig)
    assert p.wait(timeout=15) == 0
    p.stdout.close()
    p.stderr.close()
    assert wait_gone(child, grand)
    assert list(tmpdir.iterdir()) == []
    assert replies() == []


# --- single-flight ------------------------------------------------------------------------


def test_single_flight_per_agent_name(tmp_path):
    with ar.agent_lock("worker"):
        with pytest.raises(ar.AgentBusy):
            run_once(py(tmp_path, ECHO))
    # another name is independent, and the lock is released afterwards
    with ar.agent_lock("other"):
        assert run_once(py(tmp_path, ECHO)) == 0


def test_cli_second_runner_same_name_refused(tmp_path, ws):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(REPO_ROOT),
           "NEXUS_LITE_DIR": str(ws), "NEXUS_LITE_SOCK_DIR": str(tmp_path / "socks")}
    with ar.agent_lock("worker"):
        p = subprocess.run([PY, "-m", "nexus_lite", "agent", "run", "worker", "--exit-when-idle", "--", "cat"],
                           env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == ar.EXIT_BUSY
    assert "already running" in p.stderr


# --- #3 any command / #11 argv vs --shell ------------------------------------------------


def test_any_command_shell_script_and_python_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script(tmp_path, "my-agent.sh", "#!/bin/sh\nprintf 'sh:'; cat\n")
    script(tmp_path, "agent.py", "import sys\nprint('py:' + sys.stdin.read(), end='')\n")
    mtm.bus_send("worker", "one", sender="alice")
    run_once(["./my-agent.sh"])
    mtm.bus_send("worker", "two", sender="alice")
    run_once([PY, "agent.py"])
    assert sorted(m["content"] for m in replies()) == ["py:two", "sh:one"]


def test_argv_mode_no_shell_expansion_path_with_spaces(tmp_path):
    d = tmp_path / "dir with spaces $HOME"
    d.mkdir()
    s = script(d, "echo args.py", "import sys, json\nsys.stdin.read()\nprint(json.dumps(sys.argv[1:]))\n")
    mtm.bus_send("worker", "x", sender="alice")
    run_once([PY, str(s), "*", "$HOME", "a b;echo hi"])
    assert replies()[0]["content"].strip() == '["*", "$HOME", "a b;echo hi"]'


def test_shell_mode_only_when_requested(tmp_path):
    mtm.bus_send("worker", "abc", sender="alice")
    run_once("tr a-z A-Z | sed 's/^/sh:/'", shell=True)
    assert replies()[0]["content"] == "sh:ABC"
    with pytest.raises(TypeError):
        run_once("echo hi")  # a string without shell=True is refused, never split or shelled


def test_cli_argv_after_double_dash(tmp_path, ws):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(REPO_ROOT),
           "NEXUS_LITE_DIR": str(ws), "NEXUS_LITE_SOCK_DIR": str(tmp_path / "socks")}
    mtm.bus_send("worker", "hi", sender="alice")
    mtm.bus_send("worker", "yo", sender="alice")
    p = subprocess.run([PY, "-m", "nexus_lite", "agent", "run", "worker", "--exit-when-idle", "--poll", "0.05",
                        "--", *py(tmp_path, ECHO)], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    p = subprocess.run([PY, "-m", "nexus_lite", "agent", "run", "worker", "--exit-when-idle",
                        "--shell", "cat | tr a-z A-Z"], env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    _db.reset_init()
    assert sorted(m["content"] for m in replies()) == ["got:hi", "got:yo"]


def test_cli_requires_exactly_one_command_form(tmp_path, ws):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(REPO_ROOT),
           "NEXUS_LITE_DIR": str(ws)}
    for extra in ([], ["--shell", "cat", "--", "cat"]):
        p = subprocess.run([PY, "-m", "nexus_lite", "agent", "run", "worker", "--exit-when-idle", *extra],
                           env=env, capture_output=True, text=True, timeout=60)
        assert p.returncode == 2


# --- #2 runs without loom ----------------------------------------------------------------


def test_agent_run_has_no_loom_dependency():
    src = (REPO_ROOT / "nexus_lite" / "agent_run.py").read_text()
    assert "loom" not in src.lower()
    code = ("import sys, nexus_lite.agent_run, nexus_lite.cli\n"
            "bad = [m for m in sys.modules if 'loom' in m]\nassert not bad, bad\n")
    p = subprocess.run([PY, "-c", code], env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT)},
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
