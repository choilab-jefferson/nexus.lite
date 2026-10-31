"""Standalone mode: nexus_lite must run with no nexus (no NEXUS_* env, no nexus binaries)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import nexus_lite as nl

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("NEXUS_"):
            monkeypatch.delenv(k)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    monkeypatch.setenv("NEXUS_LITE_DIR", str(ws))
    return ws


def test_bus_roundtrip_without_init(standalone):
    # bus_send must create the schema itself (no prior init)
    res = nl.bus_send("worker1", "hello standalone")
    assert res["target"] == "worker1"
    msgs = nl.bus_read(target="worker1")
    assert [m["summary"] for m in msgs] == ["hello standalone"]


def test_delegate_shell_agent_without_nexus(standalone):
    res = nl.delegate(agent_cmd="cat", prompt="ping", caller="main", bg=False)
    assert res["status"] == "completed" and res["rc"] == 0
    assert "ping" in (Path(res["artifact_dir"]) / "output.md").read_text()
    # lite's own contract: identity env exported to the child
    res2 = nl.delegate(
        agent_cmd="sh -c 'echo $NEXUS_TASK_ID:$NEXUS_CALLER_ID'", prompt="x", caller="main"
    )
    out = (Path(res2["artifact_dir"]) / "output.md").read_text()
    assert f"{res2['task_id']}:main" in out


def test_cli_subprocess_clean_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "HOME": str(home),
        "PYTHONPATH": str(REPO_ROOT),
        "NEXUS_LITE_DIR": str(tmp_path / "ws"),
    }

    def run(*a):
        return subprocess.run(
            [sys.executable, "-m", "nexus_lite", *a],
            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60,
        )

    assert run("--help").returncode == 0
    p = run("bus", "send", "foo", "hi there")
    assert p.returncode == 0, p.stderr
    p = run("bus", "read", "--target", "foo")
    assert p.returncode == 0 and "hi there" in p.stdout, p.stderr
