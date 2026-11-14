"""structured `[nexus] handoff <target> - <summary>` routing (fake provider, no network)."""

import sys

import pytest

import nexus_lite.core as nl
from nexus_lite import _db, pty
from nexus_lite.memory import mtm
from nexus_lite.protocol import parse_handoffs

OUT = "\n".join([
    "working...",
    "[nexus] handoff reviewer - please review patch A",
    "[nexus] handoff bad/target - nope",
    "  [nexus] handoff tester - run suite B",
])


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    monkeypatch.setattr(pty, "list_sessions", lambda: [])
    monkeypatch.setattr(pty, "send", lambda *a, **k: None)
    _db.reset_init()
    return w


def _handoffs():
    with _db.connect() as c:
        return [(r["target"], r["sender"], r["summary"])
                for r in c.execute("SELECT target, sender, summary FROM messages WHERE kind='handoff' ORDER BY seq")]


def _fake_cmd(tmp_path):
    script = tmp_path / "fake_agent.py"
    script.write_text(f"print({OUT!r})\n", encoding="utf-8")
    return f"{sys.executable} {script}"


def test_parse_handoffs():
    assert parse_handoffs(OUT) == [
        ("reviewer", "please review patch A"), ("bad/target", "nope"), ("tester", "run suite B")]
    assert parse_handoffs("[nexus] handoff x") == []
    assert parse_handoffs(None) == []


def test_route_two_valid_one_invalid(capsys):
    assert nl._route_handoffs("t1", OUT) == 2
    assert _handoffs() == [("reviewer", "t1", "please review patch A"), ("tester", "t1", "run suite B")]
    assert "skipped" in capsys.readouterr().err


def test_delegate_default_off(tmp_path):
    res = nl.delegate(_fake_cmd(tmp_path), "go", task_id="d-off", caller="main")
    assert res["status"] == "completed"
    assert _handoffs() == []


def test_delegate_route_on(tmp_path):
    nl.delegate(_fake_cmd(tmp_path), "go", task_id="d-on", caller="main", route_handoffs=True)
    assert _handoffs() == [("reviewer", "d-on", "please review patch A"), ("tester", "d-on", "run suite B")]


def test_run_swarm_spec_route_handoffs(monkeypatch):
    seen = []

    def fake_delegate(**kw):
        seen.append(kw)
        if kw.get("route_handoffs"):
            nl._route_handoffs(kw["task_id"], OUT)
        return {"task_id": kw["task_id"]}

    monkeypatch.setattr(nl, "delegate", fake_delegate)
    monkeypatch.setattr(nl, "reap_stale_tasks", lambda: None)


    _db.init()
    specs = [{"task_id": "s-on", "cmd": "x", "prompt": "p", "route_handoffs": True},
             {"task_id": "s-off", "cmd": "x", "prompt": "p"}]
    with _db.connect() as c:
        for s in specs:
            c.execute("INSERT INTO tasks (id, caller, agent_cmd, status, rc, created_at, completed_at, artifact_dir)"
                      " VALUES (?, 'o', 'x', 'completed', 0, 0, 1, ?)", (s["task_id"], str(_db.get_workspace())))
    nl.run_swarm(specs, timeout=5)
    assert [k["route_handoffs"] for k in seen] == [True, False]
    assert _handoffs() == [("reviewer", "s-on", "please review patch A"), ("tester", "s-on", "run suite B")]
