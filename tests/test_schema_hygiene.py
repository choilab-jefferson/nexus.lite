"""Schema versioning, connection/Popen hygiene, cross-process next_seq."""

import gc
import multiprocessing as mp
import os
import sqlite3
import warnings
from pathlib import Path

import pytest

import nexus_lite.core as nl
from nexus_lite import _db
from nexus_lite.memory import ltm, mtm, stm


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_DELEGATION_DEPTH", "0")
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    _db.reset_init()
    return w


def _version(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def test_v0_db_migrates_and_keeps_data(ws):
    db = ws / "lite.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE sessions (name TEXT PRIMARY KEY, sock TEXT, cmd TEXT, created_at REAL, status TEXT)")
    con.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, sender TEXT, target TEXT, kind TEXT, summary TEXT, path TEXT, content TEXT, timestamp REAL)")
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, caller TEXT, agent_cmd TEXT, status TEXT, rc INTEGER, created_at REAL, completed_at REAL, artifact_dir TEXT)")
    con.execute("INSERT INTO messages (id, sender, summary) VALUES ('m1', 'a', 'hello')")
    con.commit()
    con.close()
    assert _version(db) == 0
    nl.init(ws)
    assert _version(db) == _db.SCHEMA_VERSION >= 1
    with _db.connect() as c:
        assert c.execute("SELECT summary FROM messages WHERE id='m1'").fetchone()["summary"] == "hello"


def test_newer_db_refused(ws):
    nl.init(ws)
    con = sqlite3.connect(str(ws / "lite.db"))
    con.execute(f"PRAGMA user_version = {_db.SCHEMA_VERSION + 5}")
    con.close()
    _db.reset_init()
    with pytest.raises(RuntimeError, match="newer"):
        nl.init(ws)
    assert _version(ws / "lite.db") == _db.SCHEMA_VERSION + 5


def test_init_memoized_and_force(ws, monkeypatch):
    nl.init(ws)
    calls = []
    real = _db.migrate
    monkeypatch.setattr(_db, "migrate", lambda c: calls.append(1) or real(c))
    nl.init(ws)
    assert calls == []
    _db.init(ws, force=True)
    assert calls == [1]


def test_connect_commits_rolls_back_and_closes(ws):
    nl.init(ws)
    with _db.connect() as c:
        c.execute("INSERT INTO messages (id) VALUES ('ok')")
    with pytest.raises(ValueError):
        with _db.connect() as c:
            c.execute("INSERT INTO messages (id) VALUES ('bad')")
            raise ValueError
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")
    with _db.connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM messages")]
    assert ids == ["ok"]


def _fds():
    return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else 0


def test_api_calls_no_leaks(ws):
    nl.init(ws)
    def burst():
        for i in range(10):
            mtm.bus_send("b", f"msg {i}", sender="a")
            ltm.wiki_write(f"slug{i}", "body")
        nl.delegate("echo hi", prompt="p", caller="t", timeout=20)
    burst()  # warm up
    gc.collect()
    before = _fds()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        burst()
        gc.collect()
    assert _fds() <= before + 1


def _seq_worker(d, n, q):
    q.put([stm.next_seq(Path(d)) for _ in range(n)])


def test_next_seq_multiprocess_unique_contiguous(tmp_path):
    d = tmp_path / "stm"
    q = mp.Queue()
    ps = [mp.Process(target=_seq_worker, args=(str(d), 50, q)) for _ in range(4)]
    for p in ps:
        p.start()
    got = [x for _ in ps for x in q.get(timeout=60)]
    for p in ps:
        p.join()
    assert sorted(got) == list(range(1, 201))
