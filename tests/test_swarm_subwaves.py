"""multi-consumer bus cursors + run_swarm dependency sub-waves (fakes, no providers)."""

import sqlite3
import time

import pytest

import nexus_lite.core as nl
from nexus_lite import _db, pty
from nexus_lite.memory import mtm


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


# --- consumers ---------------------------------------------------------------------------

def test_consumer_cursors_independent():
    s1 = mtm.bus_send("x", "one")["seq"]
    s2 = mtm.bus_send("x", "two")["seq"]
    assert mtm.bus_ack("x", s1, consumer="pty") == s1
    assert [m["seq"] for m in mtm.bus_read("x", unacked=True, consumer="pty")] == [s2]
    assert [m["seq"] for m in mtm.bus_read("x", unacked=True, consumer="turn")] == [s1, s2]
    assert [m["seq"] for m in mtm.bus_read("x", unacked=True)] == [s1, s2]
    mtm.bus_ack("x", s2)  # default cursor leaves named ones alone
    assert mtm.bus_read("x", unacked=True) == []
    assert [m["seq"] for m in mtm.bus_read("x", unacked=True, consumer="turn")] == [s1, s2]


def test_default_consumer_unchanged():
    s = mtm.bus_send("x", "a")["seq"]
    assert mtm.bus_ack("x", s) == s
    assert mtm.bus_ack("x", 0) == s  # never moves back
    assert mtm.bus_read("x", unacked=True) == []


def test_migration_old_cursor_schema(ws):
    con = sqlite3.connect(str(ws / "lite.db"))
    con.execute("CREATE TABLE sessions (name TEXT PRIMARY KEY, sock TEXT, cmd TEXT, created_at REAL, status TEXT, last_seen REAL)")
    con.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, sender TEXT, target TEXT, kind TEXT, summary TEXT, path TEXT, content TEXT, timestamp REAL, seq INTEGER)")
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, caller TEXT, agent_cmd TEXT, status TEXT, rc INTEGER, created_at REAL, completed_at REAL, artifact_dir TEXT)")
    con.execute("CREATE TABLE bus_cursors (target TEXT PRIMARY KEY, acked_seq INTEGER NOT NULL DEFAULT 0, updated REAL)")
    con.execute("CREATE TABLE bus_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    con.execute("INSERT INTO bus_cursors VALUES ('t', 7, 1.0)")
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()
    for _ in range(2):  # second init is a no-op
        _db.reset_init()
        _db.init()
        with _db.connect() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == _db.SCHEMA_VERSION >= 3
            rows = [tuple(r) for r in c.execute("SELECT target, consumer, acked_seq FROM bus_cursors")]
            assert rows == [("t", "", 7)]
    assert mtm.bus_ack("t", 9, consumer="pty") == 9


def test_prune_waits_for_all_consumers():
    s = mtm.bus_send("x", "m")["seq"]
    mtm.bus_send("x", "newer")
    mtm.bus_ack("x", s)
    mtm.bus_ack("x", 0, consumer="turn")  # turn consumer exists, has acked nothing
    assert mtm.bus_prune(0) == 0
    mtm.bus_ack("x", s, consumer="turn")
    assert mtm.bus_prune(0) == 1
    assert [m["summary"] for m in mtm.bus_read("x")] == ["newer"]


# --- sub-waves ---------------------------------------------------------------------------

@pytest.fixture
def fake_wave(monkeypatch):
    """Replace the dispatch/poll layer: record each wave, echo prompt as output."""
    waves = []

    def wave(specs, caller, timeout, verbose, on_dispatch=None):
        waves.append([dict(s) for s in specs])
        for n in range(len(specs)):
            if on_dispatch is not None:
                on_dispatch(n)
        return [{"task_id": s.get("task_id") or f"t{len(waves)}-{i}", "status": "completed", "rc": 0,
                 "output": f"out:{s.get('prompt', '')}"} for i, s in enumerate(specs)]

    monkeypatch.setattr(nl, "_swarm_wave", wave)
    return waves


def test_legacy_single_wave(fake_wave):
    res = nl.run_swarm([{"task_id": "a", "prompt": "pa"}, {"task_id": "b", "prompt": "pb"}])
    assert len(fake_wave) == 1 and [r["task_id"] for r in res] == ["a", "b"]


def test_depends_on_lazy_prompt(fake_wave):
    seen = []

    def pf(results):
        seen.append([r["output"] for r in results])
        return "C"

    specs = [
        {"task_id": "C", "depends_on": ["A", "B"], "prompt_fn": pf},
        {"task_id": "A", "prompt": "pa"},
        {"task_id": "B", "prompt": "pb"},
    ]
    res = nl.run_swarm(specs)
    assert [[s["task_id"] for s in w] for w in fake_wave] == [["A", "B"], ["C"]]
    assert seen == [["out:pa", "out:pb"]]
    assert [r["task_id"] for r in res] == ["C", "A", "B"]  # spec order


@pytest.mark.parametrize("specs", [
    [{"task_id": "A", "depends_on": ["B"]}, {"task_id": "B", "depends_on": ["A"]}],
    [{"task_id": "A", "depends_on": ["nope"]}],
    [{"task_id": "A", "depends_on": ["A"]}],
])
def test_bad_graph_raises_before_dispatch(fake_wave, specs):
    with pytest.raises(ValueError):
        nl.run_swarm(specs)
    assert fake_wave == []


def test_explicit_subwave_ordering(fake_wave):
    nl.run_swarm([{"task_id": "x", "subwave": 1}, {"task_id": "y", "subwave": 0}])
    assert [[s["task_id"] for s in w] for w in fake_wave] == [["y"], ["x"]]


def test_bus_targets_and_inbox_injection(fake_wave):
    specs = [
        {"task_id": "A", "prompt": "pa", "bus_targets": ["B"]},
        {"task_id": "B", "depends_on": ["A"], "prompt": "pb", "inject_bus_inbox": True},
    ]
    nl.run_swarm(specs)
    assert fake_wave[1][0]["prompt"].startswith("[nexus] ") or "TASK_COMPLETE" in fake_wave[1][0]["prompt"]
    assert fake_wave[1][0]["prompt"].endswith("pb")
    assert mtm.bus_read("B", unacked=True, consumer="turn") == []  # acked by the turn consumer
    assert len(mtm.bus_read("B", unacked=True)) == 1  # default cursor untouched
