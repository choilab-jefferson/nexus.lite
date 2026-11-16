"""File bus (docs/specs/file-bus.md): files are the source of truth, lite.db is a rebuildable local index."""

import json
import multiprocessing as mp
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from nexus_lite import _db, filebus, pty
from nexus_lite.memory import mtm

PY = sys.executable


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    run = tempfile.mkdtemp(prefix="nxb-", dir="/tmp")  # short: an AF_UNIX path has a ~100 byte limit
    monkeypatch.setenv("NEXUS_BUS_RUN_DIR", run)
    monkeypatch.setattr(filebus._WRITER, "last_ms", 0)  # the id writer is process-global state
    monkeypatch.setattr(pty, "list_sessions", lambda: [])
    monkeypatch.setattr(pty, "send", lambda *a, **k: None)
    _db.reset_init()
    yield w
    shutil.rmtree(run, ignore_errors=True)


@pytest.fixture
def bus(ws):
    return filebus.open_bus(ws / "bus")


def _send(bus, target="t", body="x", sender="s"):
    return bus.send(target, sender=sender, kind="dm", summary=body[:100], body=body)


def _env(ws):
    return dict(os.environ, NEXUS_LITE_HOME=str(ws), NEXUS_LITE_DIR=str(ws))


# ---------------------------------------------------------------- layout, ids, names
def test_layout_version_and_message_schema(ws, bus):
    v = json.loads((ws / "bus" / "VERSION").read_text())
    assert (v["format"], v["version"], v["skew_window_ms"]) == ("nexus.filebus", 1, 300000)
    rec = _send(bus, "loom-a1", "hello\nsecond line")
    p = ws / "bus" / "msg" / "loom-a1" / f"{rec['id']}.json"
    assert filebus.ID_RE.match(rec["id"]) and p.is_file()
    assert oct(p.stat().st_mode & 0o777) == "0o600" and oct(p.parent.stat().st_mode & 0o777) == "0o700"
    on_disk = json.loads(p.read_text())
    assert {"v", "id", "sender", "target", "kind", "summary", "timestamp", "body", "body_ref", "data", "meta"} <= set(on_disk)
    assert on_disk["body"] == "hello\nsecond line" and on_disk["meta"]["origin"] == "lite"
    assert [n for n in os.listdir(p.parent)] == [p.name]  # no temp file left behind


@pytest.mark.parametrize("bad", [".hid", "_x", "a/b", "", "a" * 129, "x\x00"])
def test_name_rules(bad):
    with pytest.raises(ValueError):
        mtm.bus_send(bad, "m")
    with pytest.raises(ValueError):
        mtm.bus_register(bad)


def test_ids_are_ordered_and_writer_clamp_keeps_ahead_of_listed(bus):
    ids = [bus.new_id("t") for _ in range(50)]
    assert ids == sorted(ids) and len(set(ids)) == 50
    # a message from a fast-clocked writer is already visible: the next id never sorts below it
    future = f"m{int(time.time() * 1000) + 60000:013d}-00000000-000001"
    bus.publish_message("t", future, sender="a", kind="dm", summary="", body="f")
    bus.list_ids("t")
    assert bus.new_id("t") > future


def test_writer_clamp_ignores_own_ids_so_bursts_do_not_drift(bus):
    for _ in range(300):
        rec = _send(bus, "t")
        bus.list_ids("t")  # a listing between sends must not push the next id ahead by its own 1 ms
    assert filebus.id_ms(rec["id"]) <= int(time.time() * 1000) + 1  # never ahead of the wall clock


def test_ack_adds_the_old_hwm_so_it_is_not_redelivered(bus):
    a, b = _send(bus, "t", "a")["id"], _send(bus, "t", "b")["id"]
    bus.ack("t", None, [a])
    bus.ack("t", None, [b])
    cur = bus.read_cursor("t")
    assert cur["hwm"] == b and a in cur["acked"] and bus.unacked_ids("t") == []


def test_large_body_goes_to_a_blob(ws, bus):
    big = "é" * (filebus.MAX_BODY // 2 + 10)  # > 256 KiB in UTF-8
    r = mtm.bus_send("t", big)
    rec = json.loads((ws / "bus" / "msg" / "t" / f"{r['id']}.json").read_text())
    assert rec["body"] is None and rec["body_ref"] == f"blob/t/{r['id']}.txt"
    assert (ws / "bus" / rec["body_ref"]).is_file()
    assert mtm.bus_read("t", unacked=True)[0]["content"] == big


def test_legacy_inbox_body_still_written_and_path_points_at_it(ws):
    r = mtm.bus_send("t", "body")
    assert r["path"] == str(ws / "inbox" / f"{r['id']}.md") and "body" in (ws / "inbox" / f"{r['id']}.md").read_text()
    assert mtm.bus_read("t", unacked=True)[0]["path"] == r["path"]


def test_all_is_case_insensitive_and_merged_into_every_inbox(ws):
    mtm.bus_send("all", "hi", kind="broadcast")
    assert (ws / "bus" / "msg" / "ALL").is_dir()
    assert [m["content"] for m in mtm.bus_read("anyone", unacked=True)] == ["hi"]


def test_future_layout_version_refused(ws):
    b = ws / "bus"
    b.mkdir()
    (b / "VERSION").write_text(json.dumps({"format": "nexus.filebus", "version": 2, "minor": 0}))
    with pytest.raises(filebus.BusLayoutError, match="version 2 unsupported"):
        mtm.bus_send("t", "x")
    r = subprocess.run([PY, "-m", "nexus_lite", "bus", "wait", "--target", "t", "--timeout", "1"],
                       env=_env(ws), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "unsupported" in r.stderr


# ---------------------------------------------------------------- concurrency and crashes
def _writer(root, n, q):
    b = filebus.Bus(root)
    q.put([b.send("t", sender="p", kind="dm", summary="", body=str(i))["id"] for i in range(n)])


def test_concurrent_writers_lose_nothing_and_never_collide(ws):
    root = str(ws / "bus")
    filebus.open_bus(root)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_writer, args=(root, 150, q)) for _ in range(4)]
    for p in procs:
        p.start()
    got = [i for _ in procs for i in q.get(timeout=60)]
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    assert len(got) == len(set(got)) == 600
    b = filebus.open_bus(root)
    assert b.list_ids("t") == sorted(got)
    assert all(b.read_message("t", i) for i in got)  # every file is complete JSON


def _crashing_sender(root):
    real = os.replace

    def boom(src, dst):
        if str(dst).endswith(".json"):
            os._exit(9)  # killed after the temp file was written, before the rename
        return real(src, dst)

    os.replace = boom
    filebus.Bus(root).send("t", sender="p", kind="dm", summary="", body="lost")


def test_crash_between_write_and_rename_leaves_an_invisible_orphan(ws, bus):
    ctx = mp.get_context("spawn")
    p = ctx.Process(target=_crashing_sender, args=(str(ws / "bus"),))
    p.start()
    p.join(30)
    assert p.exitcode == 9
    d = ws / "bus" / "msg" / "t"
    orphans = [n for n in os.listdir(d) if n.startswith(".tmp.")]
    assert len(orphans) == 1 and bus.list_ids("t") == [] and mtm.bus_read("t", unacked=True) == []
    ok = _send(bus, "t", "after")
    assert bus.list_ids("t") == [ok["id"]]
    assert bus.prune(0, now=time.time() + 60) == [] and (d / orphans[0]).exists()  # not 1 h old yet
    assert bus.prune(0, now=time.time() + 2 * 3600) == [] and not (d / orphans[0]).exists()


def test_partial_and_foreign_files_are_skipped_then_accepted(ws, bus):
    d = ws / "bus" / "msg" / "t"
    d.mkdir(parents=True)
    mid = f"m{int(time.time() * 1000):013d}-aaaaaaaa-000001"
    (d / f"{mid}.json").write_text('{"v": 1, "id": "' + mid + '", "sen')  # a partial write seen too early
    for foreign in ("notes.txt", ".hidden.json", "m12-bad.json", ".tmp.1.dead"):
        (d / foreign).write_text("{}")
    assert mtm.bus_read("t", unacked=True) == []
    assert bus.read_message("t", mid) is None
    good = {"v": 1, "id": mid, "sender": "x", "target": "t", "kind": "dm", "summary": "s", "timestamp": 1.0,
            "body": "now complete"}
    (d / f"{mid}.json").write_text(json.dumps(good))
    assert [m["content"] for m in mtm.bus_read("t", unacked=True)] == ["now complete"]
    wrong = dict(good, id=mid.replace("aaaaaaaa", "bbbbbbbb"), target="other")  # wrong id/target: ignored
    (d / f"{wrong['id']}.json").write_text(json.dumps(wrong))
    assert len(mtm.bus_read("t", unacked=True)) == 1


# ---------------------------------------------------------------- cursors
def test_ack_is_idempotent_monotone_and_does_not_skip_lower_ids(bus):
    a, b, c = (_send(bus, "t", x)["id"] for x in "abc")
    cur = bus.ack("t", None, [b])
    assert cur["hwm"] == b and bus.unacked_ids("t") == [a, c]  # a is below hwm but was never acked
    assert bus.ack("t", None, [b]) == bus.read_cursor("t")  # idempotent
    bus.ack("t", None, [a])
    assert bus.read_cursor("t")["hwm"] == b and bus.unacked_ids("t") == [c]
    bus.ack("t", None, [])  # never moves back
    assert bus.read_cursor("t")["hwm"] == b
    bus.ack("t", None, [c])
    assert bus.unacked_ids("t") == [] and bus.read_cursor("t")["hwm"] == c


def test_cursor_misuse_causes_redelivery_never_loss(ws, bus):
    ids = [_send(bus, "t", x)["id"] for x in "abc"]
    bus.ack("t", None, ids)
    stale = {"v": 1, "target": "t", "consumer": "_default", "hwm": ids[0], "acked": [], "updated": 0}
    (ws / "bus" / "cur" / "t" / "_default.json").write_text(json.dumps(stale))  # a restarted writer, last rename wins
    assert bus.unacked_ids("t") == ids[1:]


def test_skew_window_late_arrival_delivered_once_and_old_ones_only_audited(ws, bus):
    now = int(time.time() * 1000)
    mk = lambda ms, w: f"m{ms:013d}-{w}-000001"  # noqa: E731
    hi = mk(now, "aaaaaaaa")
    bus.publish_message("t", hi, sender="a", kind="dm", summary="", body="hi")
    bus.ack("t", None, [hi])
    late = mk(now - 60_000, "bbbbbbbb")  # inside the 300 s window below the hwm
    ancient = mk(now - 400_000, "cccccccc")  # outside it
    for i in (late, ancient):
        bus.publish_message("t", i, sender="a", kind="dm", summary="", body="b")
    assert bus.unacked_ids("t") == [late]  # delivered automatically
    assert bus.audit("t") == [ancient]  # reported, never auto-delivered (D1)
    bus.ack("t", None, [late])
    assert bus.unacked_ids("t") == [] and bus.read_cursor("t")["hwm"] == hi
    assert [m["id"] for m in mtm.bus_read("t", since_seq=0, audit=True)] == [ancient]


def test_seq_ack_never_skips_a_row_the_reader_has_not_handled(ws, bus):
    """Two writers in the same millisecond: file-id order and ingest order disagree; delivery follows seq."""
    ms = int(time.time() * 1000)
    hi, lo = (f"m{ms:013d}-ffffffff-000001", f"m{ms:013d}-00000000-000001")
    bus.publish_message("t", hi, sender="a", kind="dm", summary="", body="first ingested")
    assert [m["content"] for m in mtm.bus_read("t", unacked=True)] == ["first ingested"]
    bus.publish_message("t", lo, sender="a", kind="dm", summary="", body="second ingested")
    got = mtm.bus_read("t", unacked=True, limit=1)
    assert got[0]["content"] == "first ingested"
    mtm.bus_ack("t", got[0]["seq"])
    assert [m["content"] for m in mtm.bus_read("t", unacked=True)] == ["second ingested"]


def test_ack_by_file_id_and_since_id(ws):
    a, b, c = (mtm.bus_send("t", x) for x in "abc")
    assert [m["id"] for m in mtm.bus_read("t", unacked=True, since_id=a["id"])] == [b["id"], c["id"]]
    mtm.bus_ack("t", b["id"])
    assert [m["id"] for m in mtm.bus_read("t", unacked=True)] == [a["id"], c["id"]]
    with pytest.raises(TypeError):
        mtm.bus_ack("t", "not-an-id")


def test_prune_never_deletes_unacked_or_pinned_all(ws):
    a, b = mtm.bus_send("t", "a"), mtm.bus_send("t", "b")
    mtm.bus_ack("t", a["seq"])
    far = time.time() + 10_000
    assert mtm.bus_prune(1, now=far) == 1  # a only
    assert [m["id"] for m in mtm.bus_read("t", unacked=True)] == [b["id"]]
    assert not os.path.exists(a["path"]) and os.path.exists(b["path"])
    mtm.bus_send("ALL", "bc", kind="broadcast")
    mtm.bus_register("sleeper")  # a live session with no cursor pins ALL
    assert mtm.bus_prune(1, now=far) == 0
    mtm.bus_ack("sleeper", 0)  # a cursor, but nothing acked: the broadcast is still unread by it
    assert mtm.bus_prune(1, now=far) == 0
    mtm.bus_ack("sleeper", 10**9)
    mtm.bus_ack("t", 10**9)
    assert mtm.bus_prune(1, now=far) == 2  # b and the broadcast: every cursor has them


# ---------------------------------------------------------------- the index
def _snapshot():
    return {c: [(m["id"], m["content"]) for m in mtm.bus_read("t", unacked=True, consumer=c)]
            for c in (None, "turn", "watch")}


def test_reindex_and_deleted_db_reproduce_read_and_ack(ws):
    msgs = [mtm.bus_send("t", f"m{i}") for i in range(6)]
    mtm.bus_send("ALL", "bc", kind="broadcast")
    mtm.bus_ack("t", msgs[2]["seq"])
    mtm.bus_ack("t", msgs[4]["seq"], consumer="turn")
    before = _snapshot()
    assert [i for i, _ in before[None]] == [m["id"] for m in msgs[3:]] + [mtm.bus_read("t")[0]["id"]]
    assert mtm.bus_reindex()["messages"] == 7
    assert _snapshot() == before
    _db.reset_init()
    for suffix in ("", "-wal", "-shm"):
        (ws / f"lite.db{suffix}").unlink(missing_ok=True)  # losing the index loses no bus state
    assert _snapshot() == before
    assert mtm.bus_ack("t", msgs[4]["seq"]) >= 1


def test_foreign_writer_files_are_ingested_without_lite(ws, bus):
    rec = _send(bus, "loom-a1", "from a sandbox", sender="sbx")
    got = mtm.bus_read("loom-a1", unacked=True)
    assert [(m["id"], m["sender"], m["content"]) for m in got] == [(rec["id"], "sbx", "from a sandbox")]
    assert got[0]["path"].endswith(f"{rec['id']}.json")  # no legacy body file: the message JSON is the pointer
    os.unlink(ws / "bus" / "msg" / "loom-a1" / f"{rec['id']}.json")  # another host pruned it
    assert mtm.bus_read("loom-a1", unacked=True) == []
    with _db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM messages WHERE fid IS NOT NULL").fetchone()[0] == 0


def _legacy_db(ws):
    con = sqlite3.connect(str(ws / "lite.db"))
    con.executescript("""
        CREATE TABLE sessions (name TEXT PRIMARY KEY, sock TEXT, cmd TEXT, created_at REAL, status TEXT, last_seen REAL);
        CREATE TABLE messages (id TEXT PRIMARY KEY, sender TEXT, target TEXT, kind TEXT, summary TEXT, path TEXT,
                               content TEXT, timestamp REAL, seq INTEGER);
        CREATE TABLE tasks (id TEXT PRIMARY KEY, caller TEXT, agent_cmd TEXT, status TEXT, rc INTEGER,
                            created_at REAL, completed_at REAL, artifact_dir TEXT);
        CREATE UNIQUE INDEX idx_messages_seq ON messages(seq);
        CREATE TABLE bus_cursors (target TEXT NOT NULL, consumer TEXT NOT NULL DEFAULT '', acked_seq INTEGER NOT NULL
                                  DEFAULT 0, updated REAL, PRIMARY KEY (target, consumer));
        CREATE TABLE bus_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        INSERT INTO bus_meta VALUES ('last_seq', 5);
        INSERT INTO messages VALUES ('m-00000001','a','bob','dm','one','/x/1.md','one body',100.0,1);
        INSERT INTO messages VALUES ('m-00000002','a','bob','dm','two','/x/2.md','two body',101.0,2);
        INSERT INTO messages VALUES ('m-00000003','a','carl','dm','three','/x/3.md','three body',102.0,3);
        INSERT INTO messages VALUES ('m-00000004','a','ALL','broadcast','four','/x/4.md','four body',103.0,4);
        INSERT INTO messages VALUES ('m-00000005','a','bob','dm','five','/x/5.md','five body',104.0,5);
        INSERT INTO bus_cursors VALUES ('bob', '', 2, 1.0);
        INSERT INTO bus_cursors VALUES ('bob', 'turn', 4, 1.0);
        INSERT INTO sessions VALUES ('live1', NULL, NULL, 1.0, 'live', %f);
        INSERT INTO sessions VALUES ('gone1', NULL, NULL, 1.0, 'unregistered', 1.0);
        PRAGMA user_version = 4;
    """ % time.time())
    con.commit()
    con.close()


def test_legacy_db_bus_migrates_into_files(ws):
    _legacy_db(ws)
    # cursor bob/'' acked seq<=2, bob/turn acked seq<=4; carl has no cursor
    assert [m["id"] for m in mtm.bus_read("bob", unacked=True)] == ["m-00000004", "m-00000005"]
    assert [m["id"] for m in mtm.bus_read("bob", unacked=True, consumer="turn")] == ["m-00000005"]
    assert [m["id"] for m in mtm.bus_read("carl", unacked=True)] == ["m-00000003", "m-00000004"]
    b = filebus.open_bus(ws / "bus")
    # acked by every cursor (m-1, m-2) stays an index-only row; the other three become files
    assert len(b.list_ids("bob")) == 1 and len(b.list_ids("carl")) == 1 and len(b.list_ids("ALL")) == 1
    exported = json.loads((ws / "bus" / "msg" / "carl" / f"{b.list_ids('carl')[0]}.json").read_text())
    assert exported["meta"]["legacy_id"] == "m-00000003" and exported["body"] == "three body"
    assert [s["name"] for s in b.sessions() if s["live"]] == ["live1"]
    assert b.read_session("gone1")["status"] == "unregistered"
    assert mtm.bus_send("bob", "new")["seq"] == 6  # the index counter continues
    # a retried migration rewrites the same files (deterministic ids): no duplicates
    _db.reset_init()
    with sqlite3.connect(str(ws / "lite.db")) as c:
        c.execute("PRAGMA user_version = 4")
        c.execute("UPDATE messages SET fid = NULL WHERE id LIKE 'm-%'")
    mtm.bus_read("bob")
    assert len(b.list_ids("bob")) == 2 and len(b.list_ids("carl")) == 1 and len(b.list_ids("ALL")) == 1


# ---------------------------------------------------------------- sessions
def test_session_files_and_liveness(ws, bus):
    mtm.bus_register("s1")
    rec = json.loads((ws / "bus" / "sess" / "s1.json").read_text())
    assert (rec["status"], rec["ttl_s"]) == ("live", 300) and rec["updated"] <= time.time()
    assert bus.read_session("s1")["live"]
    p = ws / "bus" / "sess" / "s1.json"
    old = time.time() - 400
    rec["updated"] = old
    p.write_text(json.dumps(rec))
    os.utime(p, (old, old))
    assert not bus.read_session("s1")["live"]
    os.utime(p)  # a fresh mtime alone also counts (max of content time and mtime)
    assert bus.read_session("s1")["live"]
    assert mtm.bus_unregister("s1") and bus.read_session("s1")["status"] == "unregistered" and not mtm.bus_unregister("nope")
    assert mtm.bus_prune(1, sessions=True, now=time.time() + 100) == 0 and not p.exists()


# ---------------------------------------------------------------- wake-up layer
def test_notify_wakes_a_waiter_long_before_the_poll_interval(ws):
    got = []
    t = threading.Thread(target=lambda: got.append(mtm.bus_wait("bob", timeout=20, poll_s=30)))
    t.start()
    time.sleep(0.5)  # the waiter is blocked on its socket
    t0 = time.monotonic()
    mtm.bus_send("bob", "ping")
    t.join(10)
    assert got and len(got[0]) == 1 and time.monotonic() - t0 < 2.0


def test_wait_cli_is_woken_by_a_sender_in_another_process(ws):
    cmd = [PY, "-m", "nexus_lite", "bus", "wait", "--target", "bob", "--timeout", "30", "--poll-s", "30"]
    p = subprocess.Popen(cmd, env=_env(ws), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    t0 = time.monotonic()
    mtm.bus_send("bob", "ping")
    out, err = p.communicate(timeout=20)
    assert p.returncode == 0 and "ping" in out and time.monotonic() - t0 < 5.0, err


def test_lost_notify_is_only_latency(ws, monkeypatch):
    monkeypatch.setattr(filebus, "_notify", lambda *a, **k: (_ for _ in ()).throw(OSError("no sockets here")))
    t = threading.Timer(0.2, lambda: mtm.bus_send("bob", "ping"))
    t.start()
    got = mtm.bus_wait("bob", timeout=10, poll_s=0.1)
    t.join()
    assert len(got) == 1


def test_wait_exit_codes_and_bus_dir_option(ws, tmp_path):
    other = tmp_path / "other-bus"
    base = [PY, "-m", "nexus_lite", "bus", "--bus-dir", str(other)]
    r = subprocess.run(base + ["wait", "--target", "bob", "--timeout", "0.3"], env=_env(ws), capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 124
    r = subprocess.run(base + ["send", "bob", "hello"], env=_env(ws), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and list((other / "msg" / "bob").glob("m*.json"))
    r = subprocess.run(base + ["wait", "--target", "bob", "--timeout", "5"], env=_env(ws), capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0 and "hello" not in r.stderr
    assert mtm.bus_read("bob", unacked=True) == []  # the default bus dir never saw it


def test_tcp_notify_is_loopback_only_and_wakes_a_waiter(ws):
    assert filebus._is_loopback("127.0.0.1") and filebus._is_loopback("::1") and not filebus._is_loopback("10.1.2.3")
    with filebus.Waiter(ws / "bus", "x", tcp=True) as w:
        assert w.tcp_addr and w.tcp_addr.startswith("127.0.0.1:")
        filebus._tcp_ping("0.0.0.0:9", b"m")  # not loopback: refused without a connection attempt
        threading.Timer(0.2, filebus._tcp_ping, args=(w.tcp_addr, b"m1")).start()
        t0 = time.monotonic()
        assert w.wait(10) and time.monotonic() - t0 < 5
    with filebus.Waiter(ws / "bus", "y") as w2:  # a session file may advertise a datagram socket as well
        mtm.bus_register("y", notify={"unix": w2.path})
        t0 = time.monotonic()
        threading.Timer(0.2, mtm.bus_send, args=("y", "hi")).start()
        assert w2.wait(10) and time.monotonic() - t0 < 5


# ---------------------------------------------------------------- shared `job` kind
def test_job_start_notify_sends_one_job_message(ws, tmp_path):
    from nexus_lite import jobs

    root = tmp_path / "jobs"
    jobs.start("train-a", [PY, "-c", "print('hi')"], root=root, cwd=str(tmp_path), notify="boss")
    jobs.wait("train-a", root=root, timeout=30)
    got = mtm.bus_read("boss", unacked=True)  # job wait returned: the message is already on the bus
    assert [m["kind"] for m in got] == ["job"] and got[0]["summary"] == "train-a OK rc=0"
    rec = json.loads(next((ws / "bus" / "msg" / "boss").glob("m*.json")).read_text())
    assert rec["body"] == "" and rec["data"]["name"] == "train-a" and rec["data"]["verdict"] == "OK"
    assert rec["data"]["rc"] == 0 and rec["data"]["run_dir"] == str((root / "train-a").resolve())
    assert rec["data"]["supervisor"] == "nexus-lite" and isinstance(rec["data"]["ts"], float)
    jobs.wait("train-a", root=root, timeout=30)
    assert len(mtm.bus_read("boss", unacked=True)) == 1  # sent once per terminal transition

    jobs.start("bad", [PY, "-c", "import sys; sys.exit(3)"], root=root, cwd=str(tmp_path), notify="boss")
    jobs.wait("bad", root=root, timeout=30)
    # ordering contract: the message is sent before the rc is recorded, so it is already there: no waiting
    msgs = mtm.bus_read("boss", unacked=True)
    fail = [m for m in msgs if m["summary"].startswith("bad ")]
    assert fail and fail[0]["summary"] == "bad FAIL rc=3"


def test_job_cli_notify_flag_and_a_job_without_it_sends_nothing(ws, tmp_path):
    env = dict(_env(ws), NEXUS_LITE_JOBS_ROOT=str(tmp_path / "jobs"))
    run = lambda *a: subprocess.run([PY, "-m", "nexus_lite", "job", *a], env=env, capture_output=True, text=True,  # noqa: E731
                                    timeout=60, cwd=str(tmp_path))
    assert run("start", "quiet", "--", PY, "-c", "pass").returncode == 0
    assert run("wait", "quiet").returncode == 0
    assert run("start", "loud", "--notify", "boss", "--", PY, "-c", "pass").returncode == 0
    assert run("wait", "loud").returncode == 0
    assert [m["summary"] for m in mtm.bus_read("boss", unacked=True)] == ["loud OK rc=0"]  # already on the bus


def test_job_wait_blocks_on_a_kernel_exit_event_not_a_poll(tmp_path, monkeypatch):
    from nexus_lite import jobs

    if not hasattr(os, "pidfd_open") and not hasattr(__import__("select"), "kqueue"):
        pytest.skip("no pidfd / kqueue on this platform")
    root = tmp_path / "jobs"
    jobs.start("nap", [PY, "-c", "import time; time.sleep(1.5)"], root=root, cwd=str(tmp_path))
    sleeps = []
    monkeypatch.setattr(jobs.time, "sleep", lambda s: sleeps.append(s))
    t0 = time.monotonic()
    st = jobs.wait("nap", root=root, timeout=30)
    assert st.state == jobs.OK and time.monotonic() - t0 > 0.5
    # No polling loop (the fallback sleeps WAIT_POLL / 0.2 s). Short sub-50 ms sleeps come from the stdlib
    # (subprocess.Popen.wait(timeout=...) doubles 1, 2, 4 ... ms) and are not jobs-level polling (macOS).
    assert [s for s in sleeps if s >= 0.05] == []


def test_job_wait_falls_back_to_polling_when_identity_does_not_match(tmp_path, monkeypatch):
    from nexus_lite import jobs

    root = tmp_path / "jobs"
    jobs.start("fb", [PY, "-c", "import time; time.sleep(1)"], root=root, cwd=str(tmp_path))
    d = root / "fb"
    assert jobs._block_until_exit(int((d / "pid").read_text()), d, 0.01) in (True, False)
    (d / "pidstart").write_text("0")  # the recorded identity no longer matches: no event wait on a stranger
    assert jobs._block_until_exit(int((d / "pid").read_text()), d, 0.5) is False


def test_bus_where_and_the_resolved_dir_line_on_send_and_wait(ws, tmp_path):
    run = lambda *a, **k: subprocess.run([PY, "-m", "nexus_lite", "bus", *a], capture_output=True, text=True,  # noqa: E731
                                         timeout=30, **k)
    r = run("where", env=_env(ws))
    assert r.returncode == 0 and r.stdout.strip() == str(ws.resolve() / "bus") and r.stderr == ""
    r = run("send", "bob", "hi", env=_env(ws))
    assert r.returncode == 0 and f"bus: {ws.resolve() / 'bus'}" in r.stderr.splitlines()
    r = run("wait", "--target", "bob", "--timeout", "5", env=_env(ws))
    assert r.returncode == 0 and f"bus: {ws.resolve() / 'bus'}" in r.stderr
    # flag beats env beats the workspace
    env = dict(_env(ws), NEXUS_BUS_DIR=str(tmp_path / "from-env"))
    assert run("where", env=env).stdout.strip() == str(tmp_path / "from-env")
    assert run("where", "--bus-dir", str(tmp_path / "flag"), env=env).stdout.strip() == str(tmp_path / "flag")
    # no env: `.nexus` of the cwd
    bare = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    r = run("where", env=bare, cwd=str(tmp_path))
    assert r.stdout.strip() == str((tmp_path / ".nexus" / "bus").resolve())


# ---------------------------------------------------------------- interop rules (Go store compatibility)
def test_session_numbers_that_go_parses_are_integers(ws, bus):
    mtm.bus_register("s1")
    raw = (ws / "bus" / "sess" / "s1.json").read_text()
    assert '"ttl_s":300,' in raw.replace(" ", "") and "300.0" not in raw


def test_body_ref_wins_and_empty_inline_body_is_accepted(ws, bus):
    d = ws / "bus" / "msg" / "t"
    (ws / "bus" / "blob" / "t").mkdir(parents=True)
    mid = f"m{int(time.time() * 1000):013d}-aaaaaaaa-000001"
    (ws / "bus" / "blob" / "t" / f"{mid}.txt").write_text("the real body")
    d.mkdir(parents=True)
    (d / f"{mid}.json").write_text(json.dumps({
        "v": 1, "id": mid, "sender": "go", "target": "t", "kind": "loom.result", "summary": "s", "timestamp": 1.0,
        "body": "", "body_ref": f"blob/t/{mid}.txt", "data": {"status": "ok"}}))
    got = mtm.bus_read("t", unacked=True)
    assert [m["content"] for m in got] == ["the real body"] and got[0]["data"] == {"status": "ok"}


def test_data_is_exposed_by_api_and_cli_full(ws):
    r = mtm.bus_send("boss", "", kind="job", summary="x OK rc=0", data={"name": "x", "rc": 0})
    assert r["data"] == {"name": "x", "rc": 0}
    assert mtm.bus_read("boss", unacked=True)[0]["data"] == {"name": "x", "rc": 0}
    assert mtm.bus_read("boss")[0]["data"] == {"name": "x", "rc": 0}
    assert mtm.bus_wait("boss", timeout=2)[0]["data"]["rc"] == 0
    mtm.bus_send("boss", "plain")
    assert [m["data"] for m in mtm.bus_read("boss", unacked=True)] == [{"name": "x", "rc": 0}, None]
    out = subprocess.run([PY, "-m", "nexus_lite", "bus", "read", "--target", "boss", "--unacked", "--full"],
                         env=_env(ws), capture_output=True, text=True, timeout=30).stdout
    assert 'data: {"name":"x","rc":0}' in out.splitlines() and out.count("data: ") == 1
    mtm.bus_reindex()
    assert mtm.bus_read("boss", unacked=True)[0]["data"] == {"name": "x", "rc": 0}


def test_run_dir_rule_and_every_socket_in_it_is_pinged(ws, tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setenv("NEXUS_BUS_RUN_DIR", "/tmp/nxb-rule")
    assert filebus.run_dir() == filebus.Path("/tmp/nxb-rule")
    monkeypatch.setenv("NEXUS_BUS_RUN_DIR", "/tmp/" + "x" * 80)
    assert str(filebus.run_dir()) == f"/tmp/nexus-bus-{os.getuid()}"
    monkeypatch.delenv("NEXUS_BUS_RUN_DIR")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert str(filebus.run_dir()) == "/run/user/1000/nexus-bus"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    assert str(filebus.run_dir()) == f"/var/tmp/nexus-bus-{os.getuid()}"
    run = tempfile.mkdtemp(prefix="nxb-", dir="/tmp")
    monkeypatch.setenv("NEXUS_BUS_RUN_DIR", run)
    key = hashlib.sha1(str((ws / "bus").resolve()).encode()).hexdigest()[:8]
    # a waiter of ANOTHER target (and any socket name) is woken by a send, as another implementation would bind it
    with filebus.Waiter(ws / "bus", "someone-else") as w:
        assert os.path.dirname(w.path) == os.path.join(run, key)
        foreign = os.path.join(run, key, "go-waiter.sock")
        import socket as _s
        g = _s.socket(_s.AF_UNIX, _s.SOCK_DGRAM)
        g.bind(foreign)
        g.settimeout(5)
        try:
            mtm.bus_send("bob", "ping")
            assert g.recv(128).startswith(b"m")
            assert w.wait(2)
        finally:
            g.close()
            os.unlink(foreign)
    shutil.rmtree(run, ignore_errors=True)
