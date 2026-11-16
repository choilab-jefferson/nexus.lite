"""Python <-> Go interop contract for loom loop mode, against a REAL `loom serve` (offline `mock` provider).

Skipped unless a loom binary that reports `loop_mode` in `capabilities --json` is found (LOOM_BIN, else PATH).
`loom serve` itself is the Go side of the file bus, so no Go toolchain or probe binary is needed: a message the
Python writer produced must be decoded by Go, and the Go replies must be readable by the Python bus layer.
Spec: docs/specs/file-bus.md sections 13 and 14.
"""
import hashlib
import json
import re
import subprocess
import time

import pytest

from nexus_lite import filebus
from nexus_lite.loom import LoomAdapter, find_loom, probe

LOOM = find_loom()
_PR = probe(LOOM) if LOOM else None
pytestmark = pytest.mark.skipif(
    not (_PR and _PR.available and isinstance((_PR.capabilities or {}).get("loop_mode"), dict)),
    reason="no loom binary with loop_mode (set LOOM_BIN or add to PATH)")

ID_RE = re.compile(r"^m[0-9]{13}-[0-9a-f]{8}-[0-9]{6}$")
T = 30  # generous upper bound for any single wait (replies normally take milliseconds)


@pytest.fixture
def serve(tmp_path):
    loops = []

    def make(name="L1", **opts):
        (tmp_path / "ws").mkdir(exist_ok=True)
        opts.setdefault("provider", "mock")
        opts.setdefault("workspace", tmp_path / "ws")
        loop = LoomAdapter(bin=LOOM).serve(tmp_path / "bus", name, **opts)
        loops.append(loop)
        return loop.start()

    yield make
    for lp in loops:
        lp.close()


def test_session_file_is_readable_by_python(serve, tmp_path):
    loop = serve()
    s = filebus.open_bus(tmp_path / "bus").read_session("L1")
    assert s["live"] and s["status"] == "live"
    assert s["agent"]["kind"] == "loom" and s["agent"]["loop_mode"] == 1


def test_task_result_contract_and_cursor(serve, tmp_path):
    loop = serve()
    t = loop.task("héllo wörld ☃ 한글", timeout=T)
    assert t.ok and t.status == "ok" and t.exit_code == 0
    d = t.data
    assert d["request"] == t.message_id and ID_RE.match(t.message_id)
    assert isinstance(d["turns"], int) and isinstance(d["usage"], dict) and "trace" in d
    assert t.body  # the turn's output text
    bus = filebus.open_bus(tmp_path / "bus")
    assert bus.read_cursor("L1", None)["hwm"] >= t.message_id  # loom acked the task on cursor L1/_default
    # the reply file itself: sender, corr, id grammar, v=1
    rep = [m for m in (bus.read_message("L1-host", i) for i in bus.list_ids("L1-host")) if m["kind"] == "loom.result"]
    assert len(rep) == 1 and rep[0]["v"] == 1 and rep[0]["sender"] == "L1"
    assert rep[0]["meta"]["corr"] == t.message_id and ID_RE.match(rep[0]["id"])
    # unicode survives Python writer -> Go reader -> persistent context -> extract
    ctx = loop.extract("context", timeout=T)
    assert "héllo wörld ☃ 한글" in json.dumps(ctx.obj, ensure_ascii=False)


def test_reply_to_and_prompt_alias(serve, tmp_path):
    loop = serve()
    bus = loop._bus
    rid = bus.send("L1", sender=loop.host, kind="loom.prompt", summary="s", body="", data={"prompt": "alias"},
                   meta={"reply_to": "other"})["id"]
    bus.notify("L1", rid)
    deadline = time.monotonic() + T
    got = []
    while not got and time.monotonic() < deadline:
        got = [m for m in (bus.read_message("other", i) for i in bus.list_ids("other")) if m and m["kind"] == "loom.result"]
        loop._waiter.wait(0.2)
    assert got and got[0]["meta"]["corr"] == rid and got[0]["data"]["request"] == rid


def test_inject_then_extract_context_and_summary(serve):
    loop = serve()
    loop.inject("INJECTED-MARKER-42")
    assert loop.task("next turn", timeout=T).ok
    ctx = loop.extract("context", timeout=T)
    assert ctx.schema == "loom.context.v1" and "INJECTED-MARKER-42" in json.dumps(ctx.obj)
    assert ctx.sha256 == hashlib.sha256(ctx.path.read_bytes()).hexdigest()
    summ = loop.extract("summary", timeout=T)
    assert summ.schema == "loom.summary.v1" and isinstance(summ.obj["summary"], str)
    assert loop.unclaimed == []


@pytest.mark.xfail(strict=False, reason="loom serve handleTask falls back to the inline body only and ignores "
                   "body_ref, so a >256 KiB body is rejected as an empty prompt (loom-side gap)")
def test_large_body_via_body_ref_is_read_by_go(serve):
    """Python writes a >256 KiB body as blob + body_ref (body null); an empty prompt makes loom use the body."""
    loop = serve()
    big = "A" * (300 * 1024)
    rid = loop._send("loom.task", {"prompt": ""}, body=big)
    assert loop._await(rid, T)["data"]["exit_code"] == 0
    assert "A" * 4096 in json.dumps(loop.extract("context", timeout=T).obj)


def test_wake_up_path_beats_polling(serve):
    """Both sides rescan only every 30 s, so a prompt round trip proves the datagram wake-up works both ways."""
    loop = serve(poll_interval=30, rescan=30)
    t0 = time.monotonic()
    assert loop.task("ping", timeout=20).ok
    assert time.monotonic() - t0 < 10


def test_resume_without_checkpoint_is_harness_error_and_loop_continues(serve):
    loop = serve()
    r = loop.resume(timeout=T)
    assert (r.status, r.exit_code) == ("harness_or_policy_error", 2)
    assert r.data["status"] == "harness_error" and r.data.get("reason")
    assert loop.task("still alive", timeout=T).ok


def test_job_events_reach_python(serve, tmp_path):
    loop = serve()
    assert loop.task("register the requester", timeout=T).ok  # job events go to the last requester
    jobs = tmp_path / "ws" / ".loom" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(["sleep", "5"])
    jid = "job-ab12cd34ef56"

    def write(status, code=None):
        rec = {"job_id": jid, "argv": ["sleep", "5"], "pid": sleeper.pid, "status": status, "detached": True,
               "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if code is not None:
            rec.update(exit_code=code, ended=rec["started"])
        tmp = jobs / ".tmpjob"
        tmp.write_text(json.dumps(rec))
        tmp.replace(jobs / f"{jid}.json")

    def verdicts():
        return [j["verdict"] for j in loop.jobs if j.get("job_id") == jid]

    def until(want):
        end = time.monotonic() + T
        while time.monotonic() < end and want not in verdicts():
            loop.poll(0.5)
        return verdicts()

    try:
        write("running")
        assert until("RUNNING")
        write("exited", 7)
        assert until("FAIL")[:2] == ["RUNNING", "FAIL"]
    finally:
        sleeper.kill()
        sleeper.wait()
    d = [j for j in loop.jobs if j["verdict"] == "FAIL"][0]
    assert d["rc"] == 7 and d["supervisor"] == "loom" and d["run_dir"] == f".loom/jobs/{jid}.json"
    assert isinstance(d["ts"], float)


def test_stop_unregisters_and_leaves_later_messages_unacked(serve, tmp_path):
    loop = serve()
    assert loop.task("one", timeout=T).ok
    assert loop.stop("done", timeout=T) == 0
    assert "stopped" in [s.get("state") for s in loop.states]
    bus = filebus.open_bus(tmp_path / "bus")
    assert bus.read_session("L1")["status"] != "live"
    late = bus.send("L1", sender="L1-host", kind="loom.task", summary="late", body="", data={"prompt": "late"})["id"]
    time.sleep(0.5)  # nobody is serving: it must stay owed
    assert bus.unacked_ids("L1") == [late]


def test_max_turns_budget_exhaustion(serve):
    loop = serve(max_turns=1)
    assert loop.task("first", timeout=T).ok
    t = loop.task("second", timeout=T)
    assert (t.status, t.exit_code) == ("budget", 4)
    loop.poll()
    assert [s for s in loop.states if s.get("state") == "exiting"][-1]["reason"] == "budget_exhausted"


def test_idle_timeout_exits_zero(serve):
    loop = serve(idle_timeout=1)
    assert loop.task("who to tell", timeout=T).ok  # the reply target for loom.state is the last requester
    assert loop._proc.wait(T) == 0
    loop.poll()
    assert [s for s in loop.states if s.get("state") == "exiting"][-1]["reason"] == "idle_timeout"


def test_newer_bus_major_is_refused_by_go(tmp_path):
    bus = tmp_path / "bus"
    bus.mkdir()
    (bus / "VERSION").write_text(json.dumps(
        {"format": "nexus.filebus", "version": 2, "minor": 0, "skew_window_ms": 300000, "created": 1.0}))
    p = subprocess.run([LOOM, "serve", "--bus-dir", str(bus), "--name", "X", "--provider", "mock"],
                       capture_output=True, text=True, timeout=T, stdin=subprocess.DEVNULL)
    assert p.returncode == 2 and "unsupported" in p.stderr


def test_minor_bump_and_unknown_keys_are_accepted_by_go(serve, tmp_path):
    bus = tmp_path / "bus"
    bus.mkdir()
    (bus / "VERSION").write_text(json.dumps({"format": "nexus.filebus", "version": 1, "minor": 7,
                                             "skew_window_ms": 300000, "created": 1.0, "future": {"k": 1}}))
    assert serve().task("hello", timeout=T).ok
