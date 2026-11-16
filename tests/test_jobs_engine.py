"""nexus-lite job as the one detached-job engine: opt-in memory cap with OOM attribution,
the roots index, `job log`, run-dir layout parity with xrun (`skipped`), `wait --xrun-exit-codes`, RUN_DIR.

The memory tests mirror xrun's #570 / #1093 cases; the roots tests mirror its #578 / #690 index cases."""
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from nexus_lite import _db, jobs, jobs_mem, jobs_roots
from nexus_lite.memory import mtm

PY = sys.executable
ROOT = Path(__file__).resolve().parents[1]
LINUX = sys.platform.startswith("linux")
HOG = ("import time; b = bytearray(300 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); time.sleep(60)")


@pytest.fixture
def jroot(tmp_path):
    return tmp_path / "jobs"


@pytest.fixture
def idx(monkeypatch, tmp_path):
    p = tmp_path / "idx" / "roots"
    monkeypatch.setenv("NEXUS_LITE_ROOTS_INDEX", str(p))
    return p


def _start(tmp_path, jroot, name, code=None, argv=None, **kw):
    return jobs.start(name, argv or [PY, "-c", code], root=jroot, cwd=str(tmp_path), **kw)


def _cli(tmp_path, *args, **env):
    e = {**os.environ, "PYTHONPATH": str(ROOT), "NEXUS_LITE_DIR": str(tmp_path / ".nexus"), **env}
    return subprocess.run([PY, "-m", "nexus_lite", "job", *args], capture_output=True, text=True, env=e,
                          cwd=tmp_path, timeout=120)


# ---------------------------------------------------------------- memory cap (xrun #570, #1093)
@pytest.mark.skipif(not LINUX, reason="needs Linux /proc")
def test_watchdog_kills_a_runaway_job_alone_and_attributes_it(tmp_path, jroot, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_MEM_ENFORCER", "watchdog")
    monkeypatch.setenv("NEXUS_LITE_MEM_POLL", "0.2")
    r = _start(tmp_path, jroot, "hog", HOG, mem_max="128M")
    assert r["state"] == jobs.RUNNING
    st = jobs.wait("hog", root=jroot, timeout=60)
    d = jroot / "hog"
    assert st.state == jobs.FAIL and st.rc != 0 and st.oom
    assert "memory limit exceeded" in (d / "log").read_text()
    assert "limit_kb=131072" in (d / "memkill").read_text() and "used_kb=" in (d / "memkill").read_text()
    assert "OOM" in st.detail and "memory limit exceeded" in st.line()
    meta = json.loads((d / "meta.json").read_text())
    assert meta["mem"] == {"max_kb": 131072, "enforcer": "watchdog"}  # the cap and its enforcer are recorded
    c = jobs.check("hog", root=jroot)
    assert c["oom"] is True and c["mem_max_kb"] == 131072 and c["state"] == jobs.FAIL
    assert jobs._verdict(st) == "OOM"


@pytest.mark.skipif(not LINUX, reason="needs Linux /proc")
def test_under_the_cap_the_job_is_untouched(tmp_path, jroot, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_MEM_ENFORCER", "watchdog")
    monkeypatch.setenv("NEXUS_LITE_MEM_POLL", "0.2")
    _start(tmp_path, jroot, "ok", "import time; time.sleep(1); print('fine')", mem_max="512M")
    st = jobs.wait("ok", root=jroot, timeout=60)
    assert st.state == jobs.OK and not st.oom and "fine" in (jroot / "ok" / "log").read_text()
    assert not (jroot / "ok" / "memkill").exists()


def test_no_cap_by_default_and_off_disables(tmp_path, jroot):
    _start(tmp_path, jroot, "dflt", "pass")
    _start(tmp_path, jroot, "off", "pass", mem_max="off")
    _start(tmp_path, jroot, "zero", "pass", mem_max="0")
    for n in ("dflt", "off", "zero"):
        assert "mem" not in json.loads((jroot / n / "meta.json").read_text())
        assert jobs.wait(n, root=jroot, timeout=30).state == jobs.OK


@pytest.mark.parametrize("bad", ["lots", "12Q", "-5", "0%", "101%", "G"])
def test_a_malformed_cap_is_refused(tmp_path, jroot, bad):
    with pytest.raises(jobs.JobError, match="bad mem_max"):
        _start(tmp_path, jroot, "e", "pass", mem_max=bad)
    r = _cli(tmp_path, "--root", str(jroot), "start", "e", "--mem-max", bad, "--", "true")
    assert r.returncode == 2 and "bad mem_max" in r.stderr
    assert not (jroot / "e").exists()


def test_size_parsing_units_and_percent():
    assert jobs_mem.parse_size_kb("512") == 512 and jobs_mem.parse_size_kb("64K") == 64
    assert jobs_mem.parse_size_kb("512M") == 512 * 1024 and jobs_mem.parse_size_kb("2g") == 2 * 1024 ** 2
    assert jobs_mem.parse_size_kb("1T") == 1024 ** 3
    assert jobs_mem.parse_size_kb(None) is None and jobs_mem.parse_size_kb("none") is None
    pct = jobs_mem.parse_size_kb("50%")
    assert pct and pct * 2 <= jobs_mem._machine_limit_kb() + 1


def test_no_mechanism_is_fail_soft(tmp_path, jroot, monkeypatch, capsys):
    monkeypatch.setenv("NEXUS_LITE_MEM_ENFORCER", "scope")
    monkeypatch.setenv("PATH", "/nonexistent-nosystemd")
    _start(tmp_path, jroot, "soft", "print('ran')", mem_max="128M")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") + os.pathsep + "/usr/bin:/bin")
    assert jobs.wait("soft", root=jroot, timeout=30).state == jobs.OK
    assert json.loads((jroot / "soft" / "meta.json").read_text())["mem"]["enforcer"] == "none"
    assert "no memory-cap mechanism" in capsys.readouterr().err


def _need_scope(kb=128 * 1024):
    if not (LINUX and shutil.which("systemd-run")) or not jobs_mem._scope_works(kb):
        pytest.skip("no systemd user scope with a memory controller here")


def test_scope_enforcer_hard_caps_and_attributes(tmp_path, jroot, monkeypatch):
    _need_scope()
    monkeypatch.setenv("NEXUS_LITE_MEM_ENFORCER", "scope")
    _start(tmp_path, jroot, "scope", HOG, mem_max="128M")
    st = jobs.wait("scope", root=jroot, timeout=60)
    d = jroot / "scope"
    assert json.loads((d / "meta.json").read_text())["mem"]["enforcer"] == "scope"
    assert st.state == jobs.FAIL and st.rc == 137 and st.oom  # kernel OOM kill -> 137, same as a shell
    assert "memory limit exceeded" in (d / "log").read_text() and "oom_kill=" in (d / "memkill").read_text()


def test_scope_report_does_not_depend_on_the_direct_childs_rc(tmp_path, jroot, monkeypatch):
    """xrun #1093: a multi-process job survives when the kernel kills only a child; the oom_kill increase must still
    be reported, whatever rc the job ends with."""
    _need_scope()
    monkeypatch.setenv("NEXUS_LITE_MEM_ENFORCER", "scope")
    code = (f"import subprocess, sys; p = subprocess.run([sys.executable, '-c', {HOG!r}]); "
            "print('child rc', p.returncode); sys.exit(0)")
    _start(tmp_path, jroot, "kid", code, mem_max="128M")
    st = jobs.wait("kid", root=jroot, timeout=60)
    assert st.state == jobs.OK and st.rc == 0 and st.oom  # the job's own verdict stays OK, the OOM is recorded
    assert "oom_kill=" in (jroot / "kid" / "memkill").read_text() and "memory limit exceeded" in st.detail
    assert jobs._verdict(st) == "OK"  # a job that finished OK is not relabelled


def test_scope_keeps_dollar_signs_in_the_command(tmp_path, jroot, monkeypatch):
    """systemd-run rewrites `$$` in its argv; the user's command must not go through it."""
    _need_scope()
    monkeypatch.setenv("NEXUS_LITE_MEM_ENFORCER", "scope")
    jobs.start("dollar", 'echo "pid=$$ home=${HOME:+set}"', root=jroot, cwd=str(tmp_path), mem_max="256M")
    assert jobs.wait("dollar", root=jroot, timeout=30).state == jobs.OK
    out = (jroot / "dollar" / "log").read_text()
    assert out.startswith("pid=") and out.split()[0][4:].isdigit() and "home=set" in out, out


@pytest.mark.skipif(not LINUX, reason="needs Linux /proc")
def test_oom_verdict_reaches_the_notify_message(tmp_path, jroot, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    run = tempfile.mkdtemp(prefix="nxb-", dir="/tmp")
    for k, v in (("NEXUS_LITE_HOME", ws), ("NEXUS_LITE_DIR", ws), ("NEXUS_BUS_RUN_DIR", run),
                 ("NEXUS_LITE_MEM_ENFORCER", "watchdog"), ("NEXUS_LITE_MEM_POLL", "0.2")):
        monkeypatch.setenv(k, str(v))
    _db.reset_init()
    try:
        _start(tmp_path, jroot, "hogn", HOG, mem_max="128M", notify="boss")
        jobs.wait("hogn", root=jroot, timeout=60)
        msgs = mtm.bus_read("boss", unacked=True)
        assert [m["summary"].split()[:2] for m in msgs] == [["hogn", "OOM"]]
    finally:
        shutil.rmtree(run, ignore_errors=True)
        _db.reset_init()


# ---------------------------------------------------------------- roots index (xrun #578 / #690)
def test_start_records_the_root_once_across_jobs_and_restarts(tmp_path, jroot, idx):
    for n in ("a", "b"):
        _start(tmp_path, jroot, n, "pass")
        jobs.wait(n, root=jroot, timeout=30)
    _start(tmp_path, jroot, "a", "pass")
    jobs.wait("a", root=jroot, timeout=30)
    assert idx.read_text().splitlines() == [str(jroot.resolve())]
    other = tmp_path / "other"
    _start(tmp_path, other, "c", "pass")
    assert idx.read_text().splitlines() == [str(jroot.resolve()), str(other.resolve())]


def test_index_holds_paths_only(tmp_path, jroot, idx):
    _start(tmp_path, jroot, "secretname", "print('payload')", mem_max="off")
    jobs.wait("secretname", root=jroot, timeout=30)
    text = idx.read_text()
    assert "secretname" not in text and "payload" not in text and text.strip() == str(jroot.resolve())


def test_concurrent_adds_are_deduplicated_and_never_lost(tmp_path, idx):
    roots = [str(tmp_path / f"r{i}") for i in range(6)]
    with concurrent.futures.ThreadPoolExecutor(12) as ex:
        assert all(ex.map(jobs_roots.add, roots * 4))
    lines = idx.read_text().splitlines()
    assert sorted(lines) == sorted(roots)  # every root present exactly once, no torn line


def test_index_tolerates_torn_and_blank_lines_and_prune_drops_only_gone_dirs(tmp_path, idx):
    live, gone = tmp_path / "live", tmp_path / "gone"
    live.mkdir()
    idx.parent.mkdir(parents=True)
    idx.write_text(f"{live}\n\n{gone}\nbad\0line\n{live}\n")
    assert jobs_roots.indexed_roots() == [str(live), str(gone)]
    assert jobs_roots.prune() == [str(gone)]
    assert idx.read_text().splitlines() == [str(live)]


def test_xrun_roots_index_env_is_honored(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_LITE_ROOTS_INDEX")
    monkeypatch.setenv("XRUN_ROOTS_INDEX", str(tmp_path / "shared"))
    assert jobs_roots.index_path() == tmp_path / "shared"
    assert jobs_roots.add(tmp_path / "r") and jobs_roots.indexed_roots() == [str(tmp_path / "r")]


def test_list_all_walks_indexed_roots_and_flags_a_missing_one(tmp_path, idx):
    r1, r2, gone = tmp_path / "r1", tmp_path / "r2", tmp_path / "gone"
    _start(tmp_path, r1, "one", "pass")
    _start(tmp_path, r2, "two", "pass")
    jobs_roots.add(gone)
    for r, n in ((r1, "one"), (r2, "two")):
        jobs.wait(n, root=r, timeout=30)
    out = _cli(tmp_path, "list", "--all", NEXUS_LITE_JOBS_ROOT=str(tmp_path / "none"),
               NEXUS_LITE_ROOTS_INDEX=str(idx)).stdout
    assert f"== {r1.resolve()}\none OK rc=0" in out and f"== {r2.resolve()}\ntwo OK rc=0" in out
    assert f"== {gone} (MISSING" in out


def test_status_finds_a_run_started_from_another_root_but_an_explicit_root_does_not(tmp_path, idx):
    other = tmp_path / "other"
    _start(tmp_path, other, "far", "pass")
    jobs.wait("far", root=other, timeout=30)
    env = {"NEXUS_LITE_JOBS_ROOT": str(tmp_path / "mine"), "NEXUS_LITE_ROOTS_INDEX": str(idx)}
    r = _cli(tmp_path, "status", "far", **env)
    assert r.returncode == 0 and r.stdout.startswith("far OK rc=0") and str(other.resolve()) in r.stderr
    assert _cli(tmp_path, "log", "far", **env).stdout.strip() == str((other / "far" / "log").resolve())
    assert _cli(tmp_path, "check", "far", **env).returncode == 0
    nope = _cli(tmp_path, "--root", str(tmp_path / "mine"), "status", "far", **env)
    assert nope.stdout.startswith("far UNKNOWN") and "no such job" in nope.stdout


def test_a_name_in_several_indexed_roots_is_refused_not_guessed(tmp_path, idx):
    for r in ("r1", "r2"):
        _start(tmp_path, tmp_path / r, "dup", "pass")
        jobs.wait("dup", root=tmp_path / r, timeout=30)
    r = _cli(tmp_path, "status", "dup", NEXUS_LITE_JOBS_ROOT=str(tmp_path / "mine"), NEXUS_LITE_ROOTS_INDEX=str(idx))
    assert r.returncode == 2 and "several indexed roots" in r.stderr


def test_the_default_root_wins_over_the_index(tmp_path, idx):
    mine, other = tmp_path / "mine", tmp_path / "other"
    for r in (mine, other):
        _start(tmp_path, r, "same", "pass")
        jobs.wait("same", root=r, timeout=30)
    r = _cli(tmp_path, "log", "same", NEXUS_LITE_JOBS_ROOT=str(mine), NEXUS_LITE_ROOTS_INDEX=str(idx))
    assert r.stdout.strip() == str((mine / "same" / "log").resolve())


def test_roots_verb_lists_and_prunes(tmp_path, idx):
    live, gone = tmp_path / "live", tmp_path / "gone"
    _start(tmp_path, live, "x", "pass")
    jobs.wait("x", root=live, timeout=30)
    jobs_roots.add(gone)
    out = _cli(tmp_path, "roots", NEXUS_LITE_ROOTS_INDEX=str(idx)).stdout
    assert f"{live.resolve()} runs=1 non_terminal=0" in out and f"{gone} (MISSING)" in out
    out = _cli(tmp_path, "roots", "--prune", NEXUS_LITE_ROOTS_INDEX=str(idx)).stdout
    assert f"PRUNED {gone}" in out and str(gone) + " (MISSING)" not in out


def test_an_unwritable_index_never_blocks_a_start(tmp_path, jroot, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("NEXUS_LITE_ROOTS_INDEX", str(blocker / "sub" / "roots"))  # parent is a file: cannot be created
    r = _start(tmp_path, jroot, "ok", "pass")
    assert r["state"] in (jobs.RUNNING, jobs.OK)
    assert jobs.wait("ok", root=jroot, timeout=30).state == jobs.OK
    assert "roots index" in capsys.readouterr().err


# ---------------------------------------------------------------- log, layout parity, exit codes, RUN_DIR
def test_log_prints_the_log_path(tmp_path, jroot):
    _start(tmp_path, jroot, "lg", "print('hi')")
    jobs.wait("lg", root=jroot, timeout=30)
    r = _cli(tmp_path, "--root", str(jroot), "log", "lg")
    assert r.returncode == 0 and r.stdout.strip() == str((jroot / "lg" / "log").resolve())


def test_skipped_is_written_and_both_names_are_read(tmp_path, jroot):
    out = tmp_path / "out.txt"
    out.write_text("done")
    r = _start(tmp_path, jroot, "s1", "pass", sentinel=str(out), min_size=1)
    assert r["state"] == jobs.SKIP
    d = jroot / "s1"
    assert (d / "skipped").read_text() == str(out.resolve()) and not (d / "skip").exists()
    st = jobs.status("s1", root=jroot)
    assert st.state == jobs.SKIP and st.detail == f"sentinel={out.resolve()}"
    (d / "skipped").rename(d / "skip")  # a run written before the rename
    (d / "skip").write_text(f"sentinel {out} valid")
    old = jobs.status("s1", root=jroot)
    assert old.state == jobs.SKIP and old.detail == f"sentinel {out} valid"
    # re-running moves both names into prev/, so a new run never inherits a stale skip
    out.write_text("")
    _start(tmp_path, jroot, "s1", f"open({str(out)!r}, 'w').write('done')", sentinel=str(out), min_size=1)
    assert jobs.wait("s1", root=jroot, timeout=30).state == jobs.OK
    assert not (d / "skip").exists() and not (d / "skipped").exists()


def test_reads_an_xrun_shaped_run_dir(tmp_path, jroot):
    """xrun writes ISO started/ended times and `skipped`; lite must read such a dir, not crash on it."""
    d = jroot / "xr"
    d.mkdir(parents=True)
    (d / "started").write_text("2026-11-15T10:00:00Z\n")
    (d / "ended").write_text("2026-11-15T10:00:42Z\n")
    (d / "rc").write_text("0\n")
    (d / "log").write_text("done\n")
    st = jobs.status("xr", root=jroot)
    assert st.state == jobs.OK and st.line().startswith("xr OK rc=0 runtime=42s")
    assert jobs.check("xr", root=jroot)["runtime_s"] == 42.0
    (d / "rc").unlink()
    (d / "skipped").write_text("/some/sentinel\n")
    assert jobs.status("xr", root=jroot).state == jobs.SKIP


def test_wait_xrun_exit_codes(tmp_path, jroot):
    root = ["--root", str(jroot)]
    _start(tmp_path, jroot, "ok", "pass")
    _start(tmp_path, jroot, "bad", "import sys; sys.exit(3)")
    out = tmp_path / "out.txt"
    out.write_text("done")
    _start(tmp_path, jroot, "skp", "pass", sentinel=str(out), min_size=1)
    gone = subprocess.Popen([PY, "-c", "pass"])
    gone.wait()
    g = jroot / "gone"
    g.mkdir()
    (g / "pid").write_text(str(gone.pid))
    _start(tmp_path, jroot, "nap", "import time; time.sleep(30)")
    try:
        for n, default, xrun in (("ok", 0, 0), ("bad", 1, 1), ("skp", 0, 1), ("gone", 4, 1)):
            assert _cli(tmp_path, *root, "wait", n, "--timeout", "30").returncode == default, n
            assert _cli(tmp_path, *root, "wait", n, "--timeout", "30", "--xrun-exit-codes").returncode == xrun, n
        t = _cli(tmp_path, *root, "wait", "nap", "--timeout", "0.5", "--xrun-exit-codes")
        assert t.returncode == 3 and " RUNNING " in t.stdout  # xrun's timeout code
    finally:
        jobs.stop("nap", root=jroot)


def test_run_dir_is_exported_unless_the_caller_has_one(tmp_path, jroot, monkeypatch):
    code = "import os; print('RD=' + os.environ.get('RUN_DIR', ''))"
    _start(tmp_path, jroot, "own", code)
    jobs.wait("own", root=jroot, timeout=30)
    assert f"RD={jroot.resolve() / 'own'}" in (jroot / "own" / "log").read_text()
    monkeypatch.setenv("RUN_DIR", "/inherited")
    _start(tmp_path, jroot, "inh", code)
    jobs.wait("inh", root=jroot, timeout=30)
    assert "RD=/inherited" in (jroot / "inh" / "log").read_text()
    _start(tmp_path, jroot, "forced", code, own_run_dir=True)
    jobs.wait("forced", root=jroot, timeout=30)
    assert f"RD={jroot.resolve() / 'forced'}" in (jroot / "forced" / "log").read_text()
    _start(tmp_path, jroot, "envwins", code, own_run_dir=True, env={"RUN_DIR": "/explicit"})
    jobs.wait("envwins", root=jroot, timeout=30)
    assert "RD=/explicit" in (jroot / "envwins" / "log").read_text()


# ---------------------------------------------------------------- start / wait / check parity (xrun #578)
def test_live_name_attaches_only_the_identical_cmd_from_the_identical_cwd(tmp_path, jroot):
    argv = [PY, "-c", "import time; time.sleep(30)"]
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    try:
        assert jobs.start("live", argv, root=jroot, cwd=str(tmp_path))["state"] == jobs.RUNNING
        assert jobs.start("live", argv, root=jroot, cwd=str(tmp_path))["state"] == "already started"
        with pytest.raises(jobs.JobError, match="another command or cwd"):
            jobs.start("live", [PY, "-c", "pass"], root=jroot, cwd=str(tmp_path))
        with pytest.raises(jobs.JobError, match="another command or cwd"):
            jobs.start("live", argv, root=jroot, cwd=str(other_cwd))
    finally:
        jobs.stop("live", root=jroot)


def test_a_recycled_pid_is_gone_and_never_signalled(tmp_path, jroot):
    sleeper = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
    try:
        d = jroot / "stranger"
        d.mkdir(parents=True)
        (d / "pid").write_text(str(sleeper.pid))
        (d / "pidstart").write_text("1")  # not this process's start time
        (d / "boot_id").write_text(jobs.boot_id())
        st = jobs.status("stranger", root=jroot)
        assert st.state == jobs.GONE and "pidstart" in st.detail
        with pytest.raises(jobs.JobError, match="refusing to signal"):
            jobs.stop("stranger", root=jroot)
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()


def test_check_reports_state_rc_runtime_and_error_lines(tmp_path, jroot):
    _start(tmp_path, jroot, "chk", "import sys; print('ok'); print('Traceback boom 12'); sys.exit(2)")
    jobs.wait("chk", root=jroot, timeout=30)
    c = jobs.check("chk", root=jroot)
    assert c["state"] == jobs.FAIL and c["rc"] == 2 and c["error_lines"] == 1 and c["runtime_s"] is not None
    assert c["oom"] is False and c["mem_max_kb"] is None


# ---------------------------------------------------------------- supervisor isolation (#48)
BROKEN = "raise ImportError('shadowed stdlib module was imported')\n"
SHOW_PP = "import os; print('PP=' + repr(os.environ.get('PYTHONPATH')))"


def test_a_stale_backport_in_lites_site_dir_cannot_shadow_the_stdlib(tmp_path):
    """The host failure: lite lives in a site dir that also holds a py3.6 `dataclasses` backport. Lite's parent must
    never be PREPENDED to PYTHONPATH, or the backport beats the stdlib in the supervisor."""
    site = tmp_path / "site"
    shutil.copytree(ROOT / "nexus_lite", site / "nexus_lite", ignore=shutil.ignore_patterns("__pycache__"))
    for mod in ("dataclasses", "json", "typing_extensions"):
        (site / f"{mod}.py").write_text(BROKEN)
    driver = (
        "import importlib.util, sys\n"
        f"site = {str(site)!r}\n"
        "sys.path.append(site)  # a real site dir comes after the stdlib\n"
        "spec = importlib.util.spec_from_file_location('nexus_lite', site + '/nexus_lite/__init__.py',\n"
        "                                              submodule_search_locations=[site + '/nexus_lite'])\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['nexus_lite'] = m; spec.loader.exec_module(m)\n"
        "from nexus_lite import jobs\n"
        "assert jobs.__file__.startswith(site), jobs.__file__\n"
        f"r = jobs.start('t', ['echo', 'ran'], root={str(tmp_path / 'jobs')!r}, cwd={str(tmp_path)!r})\n"
        "print(r['state'])\n"
        f"st = jobs.wait('t', root={str(tmp_path / 'jobs')!r}, timeout=60)\n"
        "print(st.state)\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["NEXUS_LITE_DIR"] = str(tmp_path / ".nexus")
    r = subprocess.run([PY, "-I", "-c", driver], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split()[-1] == "OK", r.stdout
    assert "ran" in (tmp_path / "jobs" / "t" / "log").read_text()


def test_the_job_sees_exactly_the_callers_pythonpath(tmp_path, jroot, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/caller/a" + os.pathsep + "/caller/b")
    _start(tmp_path, jroot, "pp", SHOW_PP)
    assert jobs.wait("pp", root=jroot, timeout=60).state == jobs.OK
    assert (jroot / "pp" / "log").read_text().strip() == "PP=" + repr("/caller/a" + os.pathsep + "/caller/b")
    monkeypatch.delenv("PYTHONPATH")
    _start(tmp_path, jroot, "pp0", SHOW_PP)
    jobs.wait("pp0", root=jroot, timeout=60)
    assert (jroot / "pp0" / "log").read_text().strip() == "PP=None"
    _start(tmp_path, jroot, "pp1", SHOW_PP, env={"PYTHONPATH": "/from/env"})
    jobs.wait("pp1", root=jroot, timeout=60)
    assert (jroot / "pp1" / "log").read_text().strip() == "PP='/from/env'"


def test_start_returns_only_after_the_command_was_exec_d(tmp_path, jroot):
    """Removing the cwd the moment `start` returns must not make the job fail (it was rc 127)."""
    for i in range(5):
        work = tmp_path / f"work{i}"
        work.mkdir()
        r = jobs.start(f"rm{i}", ["sleep", "1"], root=jroot, cwd=str(work))
        assert (jroot / f"rm{i}" / "launched").is_file() and r["state"] == jobs.RUNNING
        shutil.rmtree(work)
        st = jobs.wait(f"rm{i}", root=jroot, timeout=60)
        assert st.state == jobs.OK and st.rc == 0, (st.state, st.rc)


def test_a_command_that_cannot_exec_reports_rc_127_not_a_hang(tmp_path, jroot):
    jobs.start("nocmd", ["/no/such/binary"], root=jroot, cwd=str(tmp_path))
    st = jobs.wait("nocmd", root=jroot, timeout=60)
    assert st.state == jobs.FAIL and st.rc == 127


def test_stray_stdlib_named_modules_in_the_job_cwd_do_not_break_the_supervisor(tmp_path, jroot):
    work = tmp_path / "work"
    work.mkdir()
    for mod in ("json", "dataclasses", "subprocess", "pathlib", "os"):
        (work / f"{mod}.py").write_text(BROKEN)
    r = jobs.start("stray", ["echo", "fine"], root=jroot, cwd=str(work))
    assert r["state"] == jobs.RUNNING or r["state"] == jobs.OK
    st = jobs.wait("stray", root=jroot, timeout=60)
    assert st.state == jobs.OK and "fine" in (jroot / "stray" / "log").read_text()


@pytest.mark.skipif(not LINUX, reason="needs Linux /proc")
def test_watchdog_job_is_also_launched_before_start_returns(tmp_path, jroot, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_MEM_ENFORCER", "watchdog")
    jobs.start("wd", ["sleep", "1"], root=jroot, cwd=str(tmp_path), mem_max="512M")
    assert (jroot / "wd" / "launched").is_file()
    assert jobs.wait("wd", root=jroot, timeout=60).state == jobs.OK
