"""LoomAdapter loop mode against a scripted fake `loom serve` (no loom binary needed).

The fake speaks docs/specs/file-bus.md section 13 over `nexus_lite.filebus`; the real binary is covered by
tests/test_loom_loop_interop.py.
"""
import hashlib
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from nexus_lite.loom import LoomAdapter, LoomLoopError

REPO = str(Path(__file__).resolve().parent.parent)

FAKE = textwrap.dedent('''\
    #!{py}
    import hashlib, json, os, sys, time
    sys.path.insert(0, {repo!r})
    from nexus_lite import filebus as fb
    a = sys.argv[1:]
    if a[:2] == ["capabilities", "--json"]:
        caps = {{"version": "dev", "exit_codes": {{"ok": 0}}}}
        if not os.environ.get("LOOM_FAKE_NOLOOP"):
            caps["loop_mode"] = {{"bus_format": "nexus.filebus", "bus_version": 1}}
        print(json.dumps(caps)); sys.exit(0)
    assert a[0] == "serve"
    opt = dict(zip(a[1::2], a[2::2]))
    bus, name = fb.open_bus(opt["--bus-dir"]), opt["--name"]
    json.dump(a, open(os.path.join(opt["--bus-dir"], "argv.json"), "w"))
    bus.register(name, agent={{"kind": "loom", "loop_mode": 1}}, pid=os.getpid())
    ctx, turns = [], 0
    def reply(m, kind, data, body="", target=None):
        r = bus.send(target or m["sender"], sender=name, kind=kind, summary=kind, body=body, data=data,
                     meta={{"corr": m["id"]}})
        bus.notify(r["target"], r["id"])
    with fb.Waiter(opt["--bus-dir"], name) as w:
        while True:
            for mid in bus.unacked_ids(name):
                m = bus.read_message(name, mid)
                if m is None:
                    continue
                d, k = m["data"] or {{}}, m["kind"]
                if k == "loom.task":
                    if d["prompt"] == "slow":
                        time.sleep(60)
                    if d["prompt"] == "budget":
                        sys.exit(4)  # unacked, like --max-turns exhaustion
                    turns += 1
                    ctx.append(d["prompt"])
                    reply(m, "loom.result", {{"request": mid, "status": "ok", "exit_code": 0, "turns": 1,
                          "usage": {{}}, "trace": "state/%s.traces/%s.jsonl" % (name, mid)}}, "done: " + d["prompt"])
                elif k == "loom.inject":
                    ctx += [x["content"] for x in d.get("messages", [])]
                elif k == "loom.extract":
                    raw = json.dumps({{"schema_version": 1, "messages": ctx}}).encode()
                    rel = "blob/%s/%s.json" % (m["sender"], mid)
                    os.makedirs(os.path.dirname(os.path.join(opt["--bus-dir"], rel)), exist_ok=True)
                    open(os.path.join(opt["--bus-dir"], rel), "wb").write(raw)
                    reply(m, "loom.extracted", {{"request": mid, "path": rel, "schema": "loom.context.v1",
                          "sha256": hashlib.sha256(raw).hexdigest()}})
                elif k == "loom.stop":
                    bus.ack(name, None, [mid]); bus.unregister(name); sys.exit(0)
                bus.ack(name, None, [mid])
            w.wait(1.0)
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    p = tmp_path / "loom"
    p.write_text(FAKE.format(py=sys.executable, repo=REPO))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LOOM_BIN", str(p))
    return str(p)


def test_round_trip(fake, tmp_path):
    bus = tmp_path / "bus"
    with LoomAdapter().serve(bus, "L1", provider="mock", max_wall=60, idle_timeout="2m") as loop:
        t1 = loop.task("hello", timeout=20)
        assert t1.ok and t1.status == "ok" and t1.exit_code == 0 and t1.data["status"] == "ok" and t1.body == "done: hello"
        assert t1.data["request"] == t1.message_id
        assert t1.trace_path == str(bus / f"state/L1.traces/{t1.message_id}.jsonl")
        loop.inject("MARK-7")
        ex = loop.extract("context", timeout=20)
        assert ex.schema == "loom.context.v1" and ex.path.parent == (bus / "blob" / "L1-host").resolve()
        assert ex.sha256 == hashlib.sha256(ex.path.read_bytes()).hexdigest()
        assert ex.obj["messages"] == ["hello", "MARK-7"]
        assert loop.stop(timeout=20) == 0
    argv = json.loads((bus / "argv.json").read_text())
    assert argv[:5] == ["serve", "--bus-dir", str(bus.resolve()), "--name", "L1"]
    assert {"--provider", "mock", "--max-wall", "60s", "--idle-timeout", "2m"} <= set(argv)
    assert loop.unclaimed == [] and not loop.running


def test_replies_correlate_by_meta_corr(fake, tmp_path):
    with LoomAdapter().serve(tmp_path / "bus", "L2") as loop:
        a, b = loop.task("one", timeout=20), loop.task("two", timeout=20)
        assert (a.body, b.body) == ("done: one", "done: two") and a.message_id != b.message_id


def test_task_timeout_then_close(fake, tmp_path):
    with LoomAdapter().serve(tmp_path / "bus", "L3", rescan=0.1) as loop:
        t = loop.task("slow", timeout=0.5)
        assert t.status == "timeout" and t.exit_code is None and not t.ok
    assert not loop.running


def test_budget_exit_before_reply(fake, tmp_path):
    with LoomAdapter().serve(tmp_path / "bus", "L4") as loop:
        t = loop.task("budget", timeout=20)
        assert (t.status, t.exit_code, t.ok) == ("budget", 4, False)


def test_loop_mode_missing_is_a_clear_error(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_NOLOOP", "1")
    with pytest.raises(LoomLoopError, match="loop mode"):
        LoomAdapter().serve(tmp_path / "bus", "L5").start()


def test_unavailable_loom(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_BIN", str(tmp_path / "nope"))
    with pytest.raises(LoomLoopError, match="unavailable"):
        LoomAdapter().serve(tmp_path / "bus", "L6").start()


def test_inject_needs_content(fake, tmp_path):
    with pytest.raises(ValueError):
        LoomAdapter().serve(tmp_path / "bus", "L7").inject()
