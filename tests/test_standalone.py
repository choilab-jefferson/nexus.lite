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


def test_cli_read_then_ack_using_only_cli_output(tmp_path):
    """A CLI-only member (no Python library) must be able to ack what `bus read` showed it: read prints each
    message's seq, which `bus ack <target> <seq>` takes (cumulative: everything up to that seq)."""
    import re
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(REPO_ROOT),
           "NEXUS_LITE_DIR": str(tmp_path / "ws")}

    def run(*a):
        return subprocess.run([sys.executable, "-m", "nexus_lite", *a], env=env, capture_output=True, text=True,
                              timeout=60)

    run("bus", "send", "foo", "first", "--sender", "alice")
    run("bus", "send", "foo", "second", "--sender", "bob")
    out = run("bus", "read", "--target", "foo", "--unacked").stdout
    seqs = [int(s) for s in re.findall(r"\bseq=(\d+)\b", out)]
    assert len(seqs) == 2, out
    assert run("bus", "ack", "foo", str(seqs[0])).returncode == 0
    left = run("bus", "read", "--target", "foo", "--unacked").stdout
    assert "second" in left and "first" not in left
    run("bus", "ack", "foo", str(seqs[1]))
    assert run("bus", "read", "--target", "foo", "--unacked").stdout.strip() == "No messages."


def test_cli_read_shows_where_the_full_body_is_and_full_prints_it(tmp_path):
    """bus read lines carry only a one-line summary (first line, 100 chars); a CLI-only member must still be able to
    read the whole message: each line names the body file (as the PTY pointer does), and --full prints the body."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(REPO_ROOT),
           "NEXUS_LITE_DIR": str(tmp_path / "ws")}

    def run(*a):
        return subprocess.run([sys.executable, "-m", "nexus_lite", *a], env=env, capture_output=True, text=True,
                              timeout=60)

    body = "Summary statistics: " + ", ".join(f"v{i}={i}.5" for i in range(40)) + "\nsecond line: std=1.84"
    run("bus", "send", "lead", body, "--sender", "analyst")
    line = run("bus", "read", "--target", "lead").stdout
    assert "std=1.84" not in line                      # the summary alone is cut
    path = line.rsplit("body: ", 1)[1].strip().rstrip(")")
    assert "std=1.84" in Path(path).read_text()
    full = run("bus", "read", "--target", "lead", "--full").stdout
    assert "v39=39.5" in full and "second line: std=1.84" in full


def test_cli_bus_send_and_read_default_to_the_agent_itself(tmp_path):
    """Inside a member (NEXUS_AGENT set by the harness or agent run), a forgotten --sender must not become "user"
    (replies would go nowhere) and `bus read --unacked` reads the member's own inbox."""
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "PYTHONPATH": str(REPO_ROOT),
            "NEXUS_LITE_DIR": str(tmp_path / "ws")}

    def run(env, *a):
        return subprocess.run([sys.executable, "-m", "nexus_lite", *a], env=env, capture_output=True, text=True,
                              timeout=60)

    lead, analyst = dict(base, NEXUS_AGENT="lead"), dict(base, NEXUS_AGENT="analyst")
    assert run(lead, "bus", "send", "analyst", "please analyze").returncode == 0
    out = run(analyst, "bus", "read", "--unacked").stdout          # no --target: the agent's own inbox
    assert "lead -> analyst: please analyze" in out
    assert run(base, "bus", "send", "analyst", "from a person").returncode == 0   # no NEXUS_AGENT: as before
    assert "user -> analyst: from a person" in run(analyst, "bus", "read", "--unacked").stdout
