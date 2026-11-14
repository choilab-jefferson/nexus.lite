"""nexus_lite.jobs: detached jobs whose state lives on disk, so any later agent or script can read it.

Launch in a new session/process group with stdin closed; the recorded rc is ground truth; a pid counts as the job only
when its start time and the boot id match what the job recorded; an unmeasurable job is UNKNOWN, never finished."""
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from nexus_lite import jobs

PY = sys.executable


def _start(tmp_path, name, code, **kw):
    return jobs.start(name, [PY, "-c", code], root=tmp_path / "jobs", cwd=str(tmp_path), **kw)


def test_ok_job_records_rc_and_identity(tmp_path):
    _start(tmp_path, "ok", "print('hello')")
    st = jobs.wait("ok", root=tmp_path / "jobs", timeout=30)
    assert st.state == jobs.OK and st.rc == 0 and st.source == "rc"
    d = tmp_path / "jobs" / "ok"
    for f in ("pid", "pgid", "pidstart", "boot_id", "started", "ended", "rc", "log", "meta.json"):
        assert (d / f).is_file(), f
    assert "hello" in (d / "log").read_text()


def test_failing_job_is_fail_with_rc(tmp_path):
    _start(tmp_path, "bad", "import sys; sys.exit(3)")
    st = jobs.wait("bad", root=tmp_path / "jobs", timeout=30)
    assert st.state == jobs.FAIL and st.rc == 3


def test_stdin_is_closed(tmp_path):
    _start(tmp_path, "stdin", "import sys; print(repr(sys.stdin.read()))")
    assert jobs.wait("stdin", root=tmp_path / "jobs", timeout=30).state == jobs.OK
    assert "''" in (tmp_path / "jobs" / "stdin" / "log").read_text()


def test_start_returns_while_the_job_runs(tmp_path):
    t0 = time.time()
    _start(tmp_path, "slow", "import time; time.sleep(3)")
    assert time.time() - t0 < 2.5
    st = jobs.status("slow", root=tmp_path / "jobs")
    assert st.state == jobs.RUNNING and st.source == "pid"
    assert jobs.wait("slow", root=tmp_path / "jobs", timeout=30).state == jobs.OK


def test_valid_sentinel_skips_and_stale_sentinel_reruns(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text("done")
    r = _start(tmp_path, "s1", f"open({str(out)!r}, 'w').write('done')", sentinel=str(out), min_size=1)
    assert r["state"] == jobs.SKIP
    assert jobs.status("s1", root=tmp_path / "jobs").state == jobs.SKIP
    out.write_text("")  # stale: exists but fails the size assertion -> run again
    r = _start(tmp_path, "s2", f"open({str(out)!r}, 'w').write('done')", sentinel=str(out), min_size=1)
    assert r["state"] != jobs.SKIP
    assert jobs.wait("s2", root=tmp_path / "jobs", timeout=30).state == jobs.OK
    assert out.read_text() == "done"


def test_failed_assertion_overrides_rc_zero(tmp_path):
    out = tmp_path / "empty.txt"
    _start(tmp_path, "a", f"open({str(out)!r}, 'w').write('')", sentinel=str(out), min_size=10)
    st = jobs.wait("a", root=tmp_path / "jobs", timeout=30)
    assert st.state == jobs.FAIL and st.rc == 0 and "assert" in st.detail


def test_restart_of_a_live_name(tmp_path):
    code = "import time; time.sleep(5)"
    _start(tmp_path, "live", code)
    assert _start(tmp_path, "live", code)["state"] == "already started"
    with pytest.raises(jobs.JobError):
        _start(tmp_path, "live", "import time; time.sleep(6)")
    jobs.stop("live", root=tmp_path / "jobs")


def test_stop_ends_the_whole_group(tmp_path):
    child_pid = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; "
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(child_pid)!r}, 'w').write(str(p.pid)); time.sleep(60)")
    _start(tmp_path, "grp", code)
    for _ in range(50):
        if child_pid.is_file() and child_pid.read_text():
            break
        time.sleep(0.1)
    jobs.stop("grp", root=tmp_path / "jobs")
    st = jobs.wait("grp", root=tmp_path / "jobs", timeout=30)
    assert st.state in (jobs.FAIL, jobs.GONE)
    cpid = int(child_pid.read_text())
    for _ in range(50):
        if not jobs.pid_alive(cpid):
            break
        time.sleep(0.1)
    assert not jobs.pid_alive(cpid)


def test_killed_job_without_rc_is_gone(tmp_path):
    _start(tmp_path, "k", "import time; time.sleep(60)")
    d = tmp_path / "jobs" / "k"
    os.killpg(int((d / "pgid").read_text()), signal.SIGKILL)
    for _ in range(50):
        if jobs.status("k", root=tmp_path / "jobs").state == jobs.GONE:
            break
        time.sleep(0.1)
    st = jobs.status("k", root=tmp_path / "jobs")
    assert st.state == jobs.GONE and st.rc is None


def test_reused_pid_is_gone_and_never_signalled(tmp_path):
    jobs.start("r", [PY, "-c", "print(1)"], root=tmp_path / "jobs", cwd=str(tmp_path))
    jobs.wait("r", root=tmp_path / "jobs", timeout=30)
    d = tmp_path / "jobs" / "r"
    (d / "rc").unlink()                    # as if it died without recording, and its pid now names another process
    (d / "pid").write_text(str(os.getpid()))
    (d / "pgid").write_text(str(os.getpgid(0)))
    st = jobs.status("r", root=tmp_path / "jobs")
    assert st.state == jobs.GONE and "reused" in st.detail
    with pytest.raises(jobs.JobError):
        jobs.stop("r", root=tmp_path / "jobs")  # identity does not match: refuse, this test process survives


def test_no_pid_and_no_rc_is_unknown(tmp_path):
    (tmp_path / "jobs" / "u").mkdir(parents=True)
    assert jobs.status("u", root=tmp_path / "jobs").state == jobs.UNKNOWN


def test_tail_is_capped_and_list_shows_runs(tmp_path):
    _start(tmp_path, "many", "print('\\n'.join(str(i) for i in range(1000)))")
    jobs.wait("many", root=tmp_path / "jobs", timeout=30)
    lines = jobs.tail("many", 5000, root=tmp_path / "jobs")
    assert len(lines) == jobs.TAIL_MAX and lines[-1] == "999"
    assert [s.name for s in jobs.list_jobs(root=tmp_path / "jobs")] == ["many"]


@pytest.mark.parametrize("bad", ["", ".hidden", "a/b", "..", "x" * 200])
def test_names_are_plain(tmp_path, bad):
    with pytest.raises(jobs.JobError):
        _start(tmp_path, bad, "print(1)")


def test_default_root_is_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_DIR", str(tmp_path / ".nexus"))
    monkeypatch.delenv("NEXUS_LITE_JOBS_ROOT", raising=False)
    assert jobs.default_root() == (tmp_path / ".nexus" / "jobs").resolve()
    monkeypatch.setenv("NEXUS_LITE_JOBS_ROOT", str(tmp_path / "elsewhere"))
    assert jobs.default_root() == (tmp_path / "elsewhere").resolve()


def test_unmeasurable_live_pid_is_ambiguous_not_terminal(tmp_path, monkeypatch):
    _start(tmp_path, "amb", "import time; time.sleep(5)")
    monkeypatch.setattr(jobs, "pid_start", lambda pid: "")
    st = jobs.status("amb", root=tmp_path / "jobs")
    assert st.state == jobs.AMBIGUOUS and st.state not in jobs.TERMINAL
    with pytest.raises(jobs.JobError):
        jobs.stop("amb", root=tmp_path / "jobs")
    monkeypatch.undo()
    jobs.stop("amb", root=tmp_path / "jobs")


# ---------------------------------------------------------------- CLI: nexus-lite job ...
import json  # noqa: E402
import subprocess  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _cli(tmp_path, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "NEXUS_LITE_DIR": str(tmp_path / ".nexus")}
    return subprocess.run([PY, "-m", "nexus_lite", "job", *args], capture_output=True, text=True, env=env,
                          cwd=tmp_path, timeout=120)


def test_cli_start_wait_prints_one_line_and_exit_code(tmp_path):
    r = _cli(tmp_path, "start", "c1", "--", PY, "-c", "print('x' * 10)")
    assert r.returncode == 0, r.stderr
    w = _cli(tmp_path, "wait", "c1", "--timeout", "30")
    assert w.returncode == 0 and len(w.stdout.strip().splitlines()) == 1 and w.stdout.startswith("c1 OK rc=0")
    _cli(tmp_path, "start", "c2", "--", PY, "-c", "import sys; sys.exit(5)")
    w = _cli(tmp_path, "wait", "c2", "--timeout", "30")
    assert w.returncode == 1 and "FAIL rc=5" in w.stdout


def test_cli_shell_check_tail_list_stop(tmp_path):
    assert _cli(tmp_path, "start", "sh", "--shell", "--", "echo one; echo Error: two; exit 0").returncode == 0
    _cli(tmp_path, "wait", "sh", "--timeout", "30")
    chk = json.loads(_cli(tmp_path, "check", "sh").stdout)
    assert chk["state"] == "OK" and chk["lines"] == 2 and chk["error_lines"] == 1
    assert _cli(tmp_path, "tail", "sh", "1").stdout.strip() == "Error: two"
    assert _cli(tmp_path, "list").stdout.startswith("sh OK")
    _cli(tmp_path, "start", "long", "--", PY, "-c", "import time; time.sleep(30)")
    assert _cli(tmp_path, "wait", "long", "--timeout", "0.5").returncode == 3   # not terminal yet
    assert _cli(tmp_path, "stop", "long").returncode == 0
    assert _cli(tmp_path, "stop", "long").returncode == 2                      # not running: refused


def test_cli_rejects_a_bad_name(tmp_path):
    r = _cli(tmp_path, "start", "../x", "--", "true")
    assert r.returncode == 2 and "job name" in r.stderr


# ---------------------------------------------------------------- follow-ups (Cowork 106)
def test_restart_over_a_non_terminal_job_is_refused_without_force(tmp_path, monkeypatch):
    _start(tmp_path, "amb2", "import time; time.sleep(5)")
    monkeypatch.setattr(jobs, "pid_start", lambda pid: "")       # alive, identity unreadable -> AMBIGUOUS
    with pytest.raises(jobs.JobError):
        _start(tmp_path, "amb2", "print(1)")
    monkeypatch.undo()
    jobs.stop("amb2", root=tmp_path / "jobs")
    (tmp_path / "jobs" / "unk").mkdir(parents=True)
    (tmp_path / "jobs" / "unk" / "meta.json").write_text("{}")   # no pid, no rc -> UNKNOWN
    with pytest.raises(jobs.JobError):
        _start(tmp_path, "unk", "print(1)")
    assert _start(tmp_path, "unk", "print(1)", force=True)["state"] in (jobs.RUNNING, jobs.OK)


def test_a_rerun_keeps_the_previous_run(tmp_path):
    _start(tmp_path, "keep", "print('first')")
    jobs.wait("keep", root=tmp_path / "jobs", timeout=30)
    _start(tmp_path, "keep", "print('second')")
    jobs.wait("keep", root=tmp_path / "jobs", timeout=30)
    d = tmp_path / "jobs" / "keep"
    prev = sorted((d / "prev").iterdir())
    assert len(prev) == 1 and "first" in (prev[0] / "log").read_text() and (prev[0] / "rc").read_text() == "0"
    assert "second" in (d / "log").read_text()
    assert jobs.status("keep", root=tmp_path / "jobs").state == jobs.OK
    assert [s.name for s in jobs.list_jobs(root=tmp_path / "jobs")] == ["keep"]


def test_keep_limits_the_previous_runs_only_when_set(tmp_path):
    for i in range(4):
        _start(tmp_path, "k3", f"print({i})", keep=2 if i == 3 else None)
        jobs.wait("k3", root=tmp_path / "jobs", timeout=30)
    assert len(list((tmp_path / "jobs" / "k3" / "prev").iterdir())) == 2


def test_start_reports_verified_state_not_assumed_running(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_spawn_launcher", lambda d, env, cwd: None)  # the launcher never starts the job
    monkeypatch.setattr(jobs, "START_WAIT_S", 0.3)
    r = _start(tmp_path, "nopid", "print(1)")
    assert r["state"] == jobs.UNKNOWN and r.get("detail")


# ---------------------------------------------------------------- follow-ups (Cowork 110)
def test_a_forced_restart_is_not_overwritten_by_the_old_run(tmp_path, monkeypatch):
    _start(tmp_path, "fr", "import time; time.sleep(2)")
    old_pid, real = int((tmp_path / "jobs" / "fr" / "pid").read_text()), jobs.pid_start
    # the old run is AMBIGUOUS (its identity unreadable), so force restarts it without stopping it
    monkeypatch.setattr(jobs, "pid_start", lambda pid: "" if pid == old_pid else real(pid))
    r = _start(tmp_path, "fr", "import time; time.sleep(4)", force=True)
    monkeypatch.undo()
    assert r["state"] == jobs.RUNNING
    time.sleep(3)                                                 # the old run has ended and recorded its rc
    assert jobs.status("fr", root=tmp_path / "jobs").state == jobs.RUNNING
    old = [p for p in (tmp_path / "jobs" / "fr" / "prev").iterdir()]
    assert len(old) == 1 and (old[0] / "rc").read_text() == "0"
    assert jobs.wait("fr", root=tmp_path / "jobs", timeout=30).state == jobs.OK


def test_force_on_a_verified_running_job_stops_it_first(tmp_path):
    _start(tmp_path, "fs", "import time; time.sleep(30)")
    d = tmp_path / "jobs" / "fs"
    old_pid = int((d / "pid").read_text())
    _start(tmp_path, "fs", "print('new')", force=True)
    for _ in range(50):
        if not jobs.pid_alive(old_pid):
            break
        time.sleep(0.1)
    assert not jobs.pid_alive(old_pid)
    assert jobs.wait("fs", root=tmp_path / "jobs", timeout=30).state == jobs.OK


@pytest.mark.parametrize("bad", [-1, 1.5, "2"])
def test_keep_must_be_a_non_negative_int(tmp_path, bad):
    with pytest.raises(jobs.JobError):
        _start(tmp_path, "kk", "print(1)", keep=bad)
    assert _cli(tmp_path, "start", "kk", "--keep", "-1", "--", PY, "-c", "print(1)").returncode == 2


# ---------------------------------------------------------------- J2: digest
def test_digest_summarises_jobs_without_the_log(tmp_path):
    code = ("import sys\n"
            "for i in range(3): print(f'Error: file {i} missing')\n"
            "print('Error: timeout after 30s')\n"
            "print('ok line'); sys.exit(2)")
    _start(tmp_path, "d1", code)
    _start(tmp_path, "d2", "print('fine')")
    for n in ("d1", "d2"):
        jobs.wait(n, root=tmp_path / "jobs", timeout=30)
    rows = {r["name"]: r for r in jobs.digest(root=tmp_path / "jobs")}      # no names: every job under the root
    assert set(rows) == {"d1", "d2"}
    d1 = rows["d1"]
    assert d1["state"] == jobs.FAIL and d1["rc"] == 2 and d1["lines"] == 5 and d1["error_lines"] == 4
    assert d1["top_error"] == {"signature": "Error: file N missing", "count": 3}
    assert d1["assert"] is None and "missing" not in str(rows["d2"])
    assert [r["name"] for r in jobs.digest(["d2"], root=tmp_path / "jobs")] == ["d2"]


def test_cli_digest_prints_one_json_line_per_job(tmp_path):
    _cli(tmp_path, "start", "c1", "--", PY, "-c", "print(1)")
    _cli(tmp_path, "wait", "c1", "--timeout", "30")
    r = _cli(tmp_path, "digest")
    rows = [json.loads(l) for l in r.stdout.splitlines()]
    assert r.returncode == 0 and [x["name"] for x in rows] == ["c1"] and rows[0]["state"] == "OK"


# ---------------------------------------------------------------- J2: load
def test_load_counts_our_runnable_processes_against_our_quota(monkeypatch):
    monkeypatch.setattr(jobs, "_runnable", lambda user: 3)
    r = jobs.load(quota=4, need=1)
    assert r == {"used": 3, "quota": 4, "free": 1, "runnable": 3, "ok": True}
    assert jobs.load(quota=2, need=1) == {"used": 3, "quota": 2, "free": 0, "runnable": 3, "ok": False}
    monkeypatch.setenv("NEXUS_LITE_CPU_QUOTA", "8")
    assert jobs.load()["quota"] == 8
    monkeypatch.delenv("NEXUS_LITE_CPU_QUOTA")
    with pytest.raises(jobs.JobError):            # the machine's cores are not our quota: no fallback
        jobs.load()


def test_load_measures_this_machine(tmp_path):
    r = jobs.load(quota=10_000)
    assert r["runnable"] >= 0 and r["ok"]
    assert _cli(tmp_path, "load", "--quota", "10000").stdout.startswith("used=")
    assert _cli(tmp_path, "load", "--quota", "0", "--need", "1").returncode == 1
    env_free = {k: v for k, v in os.environ.items() if k != "NEXUS_LITE_CPU_QUOTA"}
    r = subprocess.run([PY, "-m", "nexus_lite", "job", "load"], capture_output=True, text=True, cwd=tmp_path,
                       env={**env_free, "PYTHONPATH": str(ROOT)}, timeout=60)
    assert r.returncode == 2


def test_load_does_not_count_itself_or_its_ps(monkeypatch):
    me = os.getpid()
    rows = f"{me} 1 R\n{me + 1} {me} R+\n{me + 2} 1 R\n{me + 3} 1 S\n"
    monkeypatch.setattr(jobs, "_ps_rows", lambda user: rows)
    assert jobs._runnable("someone") == 1


# ---------------------------------------------------------------- J2: wait-for
def _poller(tmp_path, states):
    """A command that prints the next state from `states` on each call, like a CI status query."""
    f = tmp_path / "n"
    script = (f"import pathlib; p = pathlib.Path({str(f)!r}); n = int(p.read_text()) if p.exists() else 0; "
              f"p.write_text(str(n + 1)); s = {states!r}; print('Pipeline state: ' + s[min(n, len(s) - 1)])")
    return [PY, "-c", script]


def test_wait_for_polls_until_a_terminal_state(tmp_path):
    cmd = _poller(tmp_path, ["running", "running", "success"])
    r = jobs.wait_for(cmd, until=["success", "failed"], ok=["success"], pattern=r"Pipeline state: (\w+)",
                      interval=0.05, timeout=10)
    assert r["state"] == "success" and r["ok"] and r["polls"] == 3


def test_wait_for_reports_a_bad_terminal_state_and_a_timeout(tmp_path):
    r = jobs.wait_for(_poller(tmp_path, ["failed"]), until=["success", "failed"], ok=["success"],
                      pattern=r"Pipeline state: (\w+)", interval=0.05, timeout=10)
    assert r["state"] == "failed" and not r["ok"]
    (tmp_path / "t").mkdir()
    r = jobs.wait_for(_poller(tmp_path / "t", ["running"]), until=["success"], ok=["success"], pattern=r"Pipeline state: (\w+)", interval=0.05, timeout=0.3)
    assert r["state"] == "TIMEOUT" and r["last"] == "running"


def test_wait_for_caps_what_it_reports_and_respects_the_timeout(tmp_path):
    big = [PY, "-c", "print('x' * 5000); print('second line')"]
    r = jobs.wait_for(big, until=["done"], interval=0.05, timeout=0.3)
    assert r["state"] == "TIMEOUT" and len(r["last"]) <= jobs.WAITFOR_LAST_MAX and "second" not in r["last"]
    slow = [PY, "-c", "import time; time.sleep(5)"]
    t0 = time.time()
    r = jobs.wait_for(slow, until=["done"], interval=0.05, timeout=1)
    assert r["state"] == "TIMEOUT" and time.time() - t0 < 3


def test_cli_wait_for_prints_one_line_and_exit_code(tmp_path):
    cmd = _poller(tmp_path, ["running", "success"])
    r = _cli(tmp_path, "wait-for", "--until", "success,failed", "--ok", "success", "--pattern",
             r"Pipeline state: (\w+)", "--interval", "0.05", "--timeout", "10", "--", *cmd)
    assert r.returncode == 0 and len(r.stdout.strip().splitlines()) == 1 and r.stdout.startswith("WAITFOR success")
    assert _cli(tmp_path, "wait-for", "--until", "x", "--", "true").returncode == 2   # interval/timeout required
