"""Bus v1 semantics: integer seq, cursor/ack, live registry, retention."""

import multiprocessing as mp
import sqlite3
import time

import pytest

from nexus_lite import _db, pty
from nexus_lite.memory import mtm


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    _db.reset_init()
    return w


@pytest.fixture
def sent(monkeypatch):
    """Capture PTY injections; every name in `alive` counts as a running pty session."""
    log, alive = [], []
    monkeypatch.setattr(pty, "list_sessions", lambda: [{"name": n, "sock": f"/s/{n}", "alive": True} for n in alive])
    monkeypatch.setattr(pty, "send", lambda name, text, enter=True: log.append((name, text)))
    return log, alive


def test_offline_target_delivered_once_on_register(sent):
    log, alive = sent
    m = mtm.bus_send("bob", "hello", sender="al")
    assert m["seq"] == 1 and log == []
    alive.append("bob")
    assert mtm.bus_register("bob")["redelivered"] == [m["id"]]
    assert len(log) == 1 and m["id"] in log[0][1]
    mtm.bus_register("bob")  # heartbeat while live: no redelivery
    assert len(log) == 1


def test_acked_not_redelivered(sent):
    log, alive = sent
    alive.append("bob")
    m = mtm.bus_send("bob", "one")
    m2 = mtm.bus_send("bob", "two")
    assert [x["id"] for x in mtm.bus_read("bob", unacked=True)] == [m["id"], m2["id"]]
    assert mtm.bus_ack("bob", m["seq"]) == m["seq"]
    assert mtm.bus_ack("bob", 0) == m["seq"]  # never moves back
    mtm.bus_unregister("bob")
    log.clear()
    assert mtm.bus_register("bob")["redelivered"] == [m2["id"]]
    mtm.bus_ack("bob", m2["seq"])
    mtm.bus_unregister("bob")
    assert mtm.bus_register("bob")["redelivered"] == []
    assert mtm.bus_read("bob", unacked=True) == []


def test_all_skips_unregistered_keeps_live_sockets(sent):
    log, alive = sent
    alive.extend(["live", "never", "gone", "stale"])
    mtm.bus_register("live")
    mtm.bus_register("gone")
    mtm.bus_unregister("gone")
    mtm.bus_register("stale")
    with _db.connect() as c:
        c.execute("UPDATE sessions SET last_seen = ? WHERE name = 'stale'", (time.time() - mtm.LIVE_TTL_S - 5,))
    mtm.bus_send("ALL", "hi", kind="broadcast")
    # unregistered excluded; live socket w/o row ("never") and stale heartbeat ("stale") stay live
    assert [n for n, _ in log] == ["live", "never", "stale"]


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\x1b[0m", "../x", "a/b", ""])
def test_control_char_ids_rejected(bad, ws):
    for call in (
        lambda: mtm.bus_send(bad, "m"),
        lambda: mtm.bus_send("t", "m", sender=bad),
        lambda: mtm.bus_register(bad),
        lambda: mtm.bus_ack(bad, 1),
    ):
        with pytest.raises(ValueError):
            call()
    assert not list((ws / "inbox").glob("*")) if (ws / "inbox").exists() else True


def test_typing_guard():
    with pytest.raises(TypeError):
        mtm.bus_send(1, "m")
    with pytest.raises(TypeError):
        mtm.bus_send("t", b"m")
    with pytest.raises(TypeError):
        mtm.bus_ack("t", "1")


def test_prune_removes_old_acked_only(ws, sent):
    old = mtm.bus_send("bob", "old-acked")
    old_unacked = mtm.bus_send("carl", "old-unacked")
    new = mtm.bus_send("bob", "new")
    mtm.bus_ack("bob", old["seq"])
    with _db.connect() as c:
        c.execute("UPDATE messages SET timestamp = timestamp - 1000 WHERE id IN (?, ?)", (old["id"], old_unacked["id"]))
    assert mtm.bus_prune(500) == 1
    ids = {m["id"] for m in mtm.bus_read()}
    assert ids == {old_unacked["id"], new["id"]}
    assert not (ws / "inbox" / f"{old['id']}.md").exists()
    assert (ws / "inbox" / f"{new['id']}.md").exists()
    # keep_last protects even old acked messages
    mtm.bus_ack("carl", old_unacked["seq"])
    assert mtm.bus_prune(500, keep_last=5) == 0


def test_seq_not_reused_after_prune(sent):
    a = mtm.bus_send("bob", "a")
    mtm.bus_ack("bob", a["seq"])
    with _db.connect() as c:
        c.execute("UPDATE messages SET timestamp = 0")
    assert mtm.bus_prune(1) == 1
    assert mtm.bus_send("bob", "b")["seq"] == a["seq"] + 1


def _worker(ws, n, q):
    import os

    os.environ["NEXUS_LITE_HOME"] = os.environ["NEXUS_LITE_DIR"] = ws
    out = [mtm.bus_send("t", f"m{i}")["seq"] for i in range(n)]
    q.put(out)


def test_stress_seq_unique_contiguous(ws):
    mtm.bus_send("t", "seed")  # init once so workers don't race the bootstrap
    ctx = mp.get_context("spawn")
    base = 1
    for _ in range(10):
        q = ctx.Queue()
        procs = [ctx.Process(target=_worker, args=(str(ws), 50, q)) for _ in range(4)]
        for p in procs:
            p.start()
        seqs = sorted(s for _ in procs for s in q.get(timeout=60))
        for p in procs:
            p.join(30)
            assert p.exitcode == 0
        q.close()
        q.join_thread()
        assert seqs == list(range(base + 1, base + 201))
        base += 200


def test_v1_db_migrates_to_v2_with_data(ws):
    con = sqlite3.connect(str(ws / "lite.db"))
    con.execute("CREATE TABLE sessions (name TEXT PRIMARY KEY, sock TEXT, cmd TEXT, created_at REAL, status TEXT)")
    con.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, sender TEXT, target TEXT, kind TEXT, summary TEXT, path TEXT, content TEXT, timestamp REAL)")
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, caller TEXT, agent_cmd TEXT, status TEXT, rc INTEGER, created_at REAL, completed_at REAL, artifact_dir TEXT)")
    con.execute("INSERT INTO messages (id, sender, target, summary, timestamp) VALUES ('m2','a','t','second',2.0)")
    con.execute("INSERT INTO messages (id, sender, target, summary, timestamp) VALUES ('m1','a','t','first',1.0)")
    con.execute("INSERT INTO sessions (name, status) VALUES ('s', 'running')")
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()
    mtm.bus_read()
    with _db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == _db.SCHEMA_VERSION >= 2
        rows = {r["id"]: r["seq"] for r in c.execute("SELECT id, seq FROM messages")}
        assert rows == {"m1": 1, "m2": 2}
        assert c.execute("SELECT status FROM sessions WHERE name='s'").fetchone()[0] == "running"
    assert mtm.bus_send("t", "x")["seq"] == 3


def test_all_reaches_live_session_without_register(sent):
    log, alive = sent
    alive.append("spawned")
    mtm.bus_send("ALL", "hi", kind="broadcast")
    assert [n for n, _ in log] == ["spawned"]  # upgrade case: live socket, no registry row


def test_all_unregister_excludes_live_socket_then_spawn_restores(sent):
    log, alive = sent
    alive.append("s1")
    mtm.bus_register("s1")
    mtm.bus_unregister("s1")
    mtm.bus_send("ALL", "a", kind="broadcast")
    assert log == []
    _db.mark_session("s1", True)  # what pty.spawn does for a re-spawned name
    mtm.bus_send("ALL", "b", kind="broadcast")
    assert [n for n, _ in log] == ["s1"]
    log.clear()
    _db.mark_session("s1", False)  # what pty.kill does
    mtm.bus_send("ALL", "c", kind="broadcast")
    assert log == []


def test_all_dead_socket_not_live_even_if_fresh_registration(sent):
    log, alive = sent
    mtm.bus_register("ghost")  # fresh row, but no live socket
    mtm.bus_send("ALL", "hi", kind="broadcast")
    assert log == []
    alive.append("old")  # live socket with a stale heartbeat stays live
    mtm.bus_register("old")
    with _db.connect() as c:
        c.execute("UPDATE sessions SET last_seen = ? WHERE name = 'old'", (time.time() - mtm.LIVE_TTL_S - 5,))
    mtm.bus_send("ALL", "hi2", kind="broadcast")
    assert [n for n, _ in log] == ["old"]
