"""EdgeKind projections: exactly {from,to,kind,ts}, no content leaks, unknown/research kinds are not agent links."""

import sqlite3

import pytest

from nexus_lite.protocol import NON_LINK_KINDS, EdgeKind, normalize_edge, normalize_spawn

KEYS = {"from", "to", "kind", "ts"}
SECRET = "SECRET-BODY /var/u/secret/path"


def msg(kind, summary=SECRET, target="b", sender="a"):
    return {"id": "m1", "sender": sender, "target": target, "kind": kind, "summary": summary,
            "path": SECRET, "content": SECRET, "timestamp": 12.5, "extra": SECRET}


@pytest.mark.parametrize("kind", ["dm", "handoff", "broadcast", "task"])
def test_message_kinds_project_exact_keys(kind):
    e = normalize_edge(msg(kind))
    assert set(e) == KEYS
    assert e == {"from": "a", "to": "b", "kind": kind, "ts": 12.5}
    assert "SECRET" not in repr(e)


def test_receipt_and_broadcast_classification():
    assert normalize_edge(msg("task", "TASK_COMPLETE t1: ok"))["kind"] == "receipt"
    assert normalize_edge(msg("task", "TASK_FAIL t1: x"))["kind"] == "receipt"
    assert normalize_edge(msg("task", "do the thing"))["kind"] == "task"
    assert normalize_edge(msg("dm", target="ALL"))["kind"] == "broadcast"


@pytest.mark.parametrize("kind", sorted(NON_LINK_KINDS) + ["bogus", "", None, "spawn", "DM"])
def test_unknown_and_research_kinds_are_not_links(kind):
    assert normalize_edge(msg(kind)) is None


def test_missing_endpoints_are_not_links():
    assert normalize_edge(msg("dm", sender=None)) is None
    assert normalize_edge(msg("dm", target="")) is None
    assert normalize_edge(None) is None


def test_spawn_projection_no_leak():
    row = {"id": "t1", "caller": "main", "agent_cmd": SECRET, "status": "done", "rc": 0,
           "created_at": 3.0, "completed_at": 4.0, "artifact_dir": SECRET}
    e = normalize_spawn(row)
    assert set(e) == KEYS
    assert e == {"from": "main", "to": "t1", "kind": "spawn", "ts": 3.0}
    assert normalize_spawn({"id": "t1", "caller": None}) is None


def test_sqlite_rows_supported():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE messages (id, sender, target, kind, summary, path, content, timestamp)")
    con.execute("INSERT INTO messages VALUES ('m','a','b','handoff',?,?,?,1.0)", (SECRET, SECRET, SECRET))
    e = normalize_edge(con.execute("SELECT * FROM messages").fetchone())
    assert e == {"from": "a", "to": "b", "kind": "handoff", "ts": 1.0}


def test_enum_values():
    assert {k.value for k in EdgeKind} == {"dm", "handoff", "broadcast", "task", "receipt", "spawn"}
