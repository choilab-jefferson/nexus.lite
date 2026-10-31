"""Process-group kill, agy headless argv, single deadline."""

import os
import stat
import sys
import time
from pathlib import Path

import pytest

import nexus_lite.core as nl

pytestmark = pytest.mark.usefixtures("isolated_env")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    monkeypatch.setenv("NEXUS_DELEGATION_DEPTH", "0")
    nl.init(ws)
    return ws


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombie counts as dead
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def test_timeout_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "gc.pid"
    cmd = f"sh -c 'sleep 300 & echo $! > {pidfile}; wait'"
    t0 = time.monotonic()
    res = nl.delegate(agent_cmd=cmd, prompt="x", timeout=2, bg=False)
    assert time.monotonic() - t0 < 12
    assert res["status"] == "timeout"
    pid = int(pidfile.read_text())
    time.sleep(0.3)
    assert not _alive(pid)


def test_agy_argv_parsed_not_equality():
    argv = nl._agy_headless_argv("agy --model X", 30)
    assert argv[:3] == ["agy", "--model", "X"]
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--print-timeout") + 1] == "30s"
    assert argv[argv.index("--output-format") + 1] == "text"
    assert "-p" not in argv
    assert nl._agy_headless_argv("/usr/bin/agy --effort low", 5) is not None
    assert nl._agy_headless_argv("agy models", 5) is None
    assert nl._agy_headless_argv("python agy", 5) is None
    assert nl._agy_headless_argv("agy -p hi", 5) is None


def test_agy_model_cmd_gets_flags_and_200kib_prompt(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "agy"
    fake.parent.mkdir()
    out = tmp_path / "seen.txt"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"data = sys.stdin.read()\n"
        f"open({str(out)!r}, 'w').write(' '.join(sys.argv[1:]) + '\\n' + str(len(data)))\n"
        "print('ok-agy')\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    big = "A" * (200 * 1024)
    res = nl.delegate(agent_cmd="agy --model X", prompt=big, timeout=30, bg=False)
    assert res["rc"] == 0, res
    argv_line, n = out.read_text().split("\n")
    assert "--model X" in argv_line and "--print-timeout" in argv_line
    assert "--output-format" in argv_line
    assert int(n) == len(big)


def test_fallback_chain_respects_single_deadline():
    hang = f"{sys.executable} -c 'import time; time.sleep(300)'"
    t0 = time.monotonic()
    res = nl.delegate(agent_cmd=hang, prompt="x", timeout=3, fallback_cmd=hang + " ", bg=False)
    assert time.monotonic() - t0 <= 3 + 5
    assert res["status"] == "timeout"


def test_run_pytest_times_out(tmp_path):
    t = tmp_path / "test_hang.py"
    t.write_text("import time\ndef test_h():\n    time.sleep(300)\n")
    t0 = time.monotonic()
    r = nl._run_pytest([sys.executable, "-m", "pytest", str(t)], str(tmp_path), timeout=2)
    assert time.monotonic() - t0 < 12
    assert r.returncode == 124 and "TIMEOUT" in r.stderr
