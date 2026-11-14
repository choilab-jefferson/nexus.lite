"""PHI egress on the bus, private file modes, bus cursor correctness, colab/pc hygiene.

Hermetic: fake agents are `python script.py`, no network, no providers.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time

import pytest

import nexus_lite.core as nl
from nexus_lite import _db, artifacts, pty
from nexus_lite.colab import pc as pcmod
from nexus_lite.colab import tunnel
from nexus_lite.memory import mtm

SECRET = "PATIENT-JANE-DOE-MRN-12345"
HANDOFF_OUT = f"{SECRET} body\n[nexus] handoff reviewer - {SECRET} summary\n[nexus] handoff ALL - {SECRET} broadcast"


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    monkeypatch.setenv("NEXUS_DELEGATION_DEPTH", "0")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    typed: list[tuple[str, str]] = []
    monkeypatch.setattr(pty, "list_sessions", lambda: [{"name": "main", "sock": "s-main"},
                                                       {"name": "reviewer", "sock": "s-rev"}])
    monkeypatch.setattr(pty, "send", lambda name, text, **k: typed.append((name, text)))
    _db.reset_init()
    return w


@pytest.fixture
def typed(monkeypatch):
    out: list[tuple[str, str]] = []
    monkeypatch.setattr(pty, "send", lambda name, text, **k: out.append((name, text)))
    return out


def _agent(tmp_path, body: str, rc: int = 0) -> str:
    script = tmp_path / f"agent_{abs(hash(body))}.py"
    script.write_text(f"import sys\nprint({body!r})\nsys.exit({rc})\n", encoding="utf-8")
    return f"{sys.executable} {script}"


def _bus_dump(w) -> str:
    """Everything the bus persisted: messages table + inbox files."""
    with _db.connect() as c:
        rows = [tuple(r) for r in c.execute("SELECT sender, target, kind, summary, content FROM messages")]
    files = [p.read_text(encoding="utf-8") for p in (w / "inbox").glob("*.md")]
    return json.dumps([rows, files])


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---- 1. PHI egress -------------------------------------------------------------------------

@pytest.fixture
def phi(monkeypatch):
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")


def test_phi_task_complete_is_pointer_only(ws, phi, tmp_path, typed):
    res = nl.delegate(_agent(tmp_path, SECRET), "p", task_id="p-ok", caller="main")
    assert res["status"] == "completed"
    with _db.connect() as c:
        row = c.execute("SELECT content FROM messages WHERE sender='p-ok'").fetchone()
    assert row["content"] == "TASK_COMPLETE p-ok: completed"
    assert SECRET not in _bus_dump(ws)
    assert typed and all(SECRET not in t for _, t in typed)  # nothing typed into a PTY either


def test_phi_escalate_has_no_worker_reason(ws, phi, tmp_path):
    payload = json.dumps({"escalation_required": True, "reason": SECRET})
    res = nl.delegate(_agent(tmp_path, payload, rc=3), "p", task_id="p-esc", caller="main")
    assert res["status"] == "escalated"
    with _db.connect() as c:
        row = c.execute("SELECT content FROM messages WHERE sender='p-esc'").fetchone()
    assert row["content"] == "TASK_ESCALATE p-esc: escalated"
    assert SECRET not in _bus_dump(ws)


def test_non_phi_keeps_summary_and_reason(ws, tmp_path):
    nl.delegate(_agent(tmp_path, "hello world"), "p", task_id="n-ok", caller="main")
    with _db.connect() as c:
        assert c.execute("SELECT content FROM messages WHERE sender='n-ok'").fetchone()["content"] == \
            "TASK_COMPLETE n-ok: hello world"


def test_phi_handoff_pointer_only_and_all_rejected(ws, phi, tmp_path):
    nl.delegate(_agent(tmp_path, HANDOFF_OUT), "p", task_id="p-h", caller="main", route_handoffs=True)
    with _db.connect() as c:
        hand = [tuple(r) for r in c.execute("SELECT target, content FROM messages WHERE kind='handoff'")]
    assert hand == [("reviewer", "HANDOFF p-h: completed")]  # ALL refused, summary dropped
    assert SECRET not in _bus_dump(ws)


@pytest.mark.parametrize("target", ["ALL", "all", "All"])
def test_handoff_target_all_rejected_without_phi(ws, target, capsys):
    assert nl._route_handoffs("t1", f"[nexus] handoff {target} - hi\n[nexus] handoff reviewer - ok") == 1
    with _db.connect() as c:
        assert [r["target"] for r in c.execute("SELECT target FROM messages WHERE kind='handoff'")] == ["reviewer"]
    assert "ALL" in capsys.readouterr().err


def test_route_handoffs_never_raises_on_bus_failure(ws, monkeypatch, capsys):
    def boom(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(nl._mtm, "bus_send", boom)
    assert nl._route_handoffs("t1", "[nexus] handoff reviewer - x\n[nexus] handoff tester - y") == 0
    assert capsys.readouterr().err.count("skipped") == 2


def test_phi_bus_targets_pointer_only(ws, phi, monkeypatch):
    monkeypatch.setattr(nl, "_swarm_wave", lambda specs, caller, timeout, verbose, on_dispatch=None: [
        {"task_id": s["task_id"], "status": "completed", "rc": 0, "output": f"{SECRET}\nmore"} for s in specs])
    nl.run_swarm([{"task_id": "A", "prompt": "p", "bus_targets": ["B"]}])
    with _db.connect() as c:
        assert c.execute("SELECT content FROM messages WHERE target='B'").fetchone()["content"] == \
            "TASK_COMPLETE A: completed"
    assert SECRET not in _bus_dump(ws)


def test_phi_pipeline_handoff_has_no_stage_output(ws, phi, tmp_path):
    stages = [{"name": "s1", "cmd": _agent(tmp_path, SECRET)}, {"name": "s2", "cmd": _agent(tmp_path, "x")}]
    out = nl.run_pipeline(stages, "go", caller="main")
    assert len(out["stages"]) == 2
    with _db.connect() as c:
        assert c.execute("SELECT content FROM messages WHERE kind='handoff'").fetchone()["content"] == \
            "HANDOFF s1 to s2: completed"
    assert SECRET not in _bus_dump(ws)


def test_mtm_redacts_unsafe_task_text_under_phi(ws, phi, typed):
    m = mtm.bus_send("main", f"free text {SECRET}", sender="w", kind="task")
    assert m["summary"] == mtm.PHI_REDACTED
    assert SECRET not in _bus_dump(ws) and all(SECRET not in t for _, t in typed)
    assert mtm.bus_send("main", "TASK_COMPLETE t-1: completed", sender="t-1", kind="task")["summary"] == \
        "TASK_COMPLETE t-1: completed"
    assert mtm.bus_send("main", "user text", kind="dm")["summary"] == "user text"  # other kinds untouched


# ---- 2. permissions ------------------------------------------------------------------------

def test_workspace_artifacts_and_inbox_modes(ws, tmp_path):
    old = os.umask(0)
    try:
        ws.chmod(0o755)  # a pre-existing loose workspace is tightened
        nl.delegate(_agent(tmp_path, "hi"), "p", task_id="m-1", caller="main")
        mtm.bus_send("main", "hello")
    finally:
        os.umask(old)
    for d in (ws, ws / "inbox", ws / "artifacts", ws / "wiki", ws / "artifacts" / "m-1"):
        assert _mode(d) == 0o700, d
    for f in ("prompt.md", "output.md", "result.md", "meta.json", "rc", "timeout", "runner.pid"):
        assert _mode(ws / "artifacts" / "m-1" / f) == 0o600, f
    inbox = list((ws / "inbox").glob("*.md"))
    assert inbox and all(_mode(p) == 0o600 for p in inbox)


def test_write_private_tightens_existing_file(tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    p.chmod(0o644)
    artifacts.write_private(p, "new")
    assert p.read_text() == "new" and _mode(p) == 0o600
    artifacts.write_private(p, b"+more", append=True)
    assert p.read_bytes() == b"new+more"


def test_token_file_and_dir_modes(tmp_path):
    p = tunnel._write_token_file("k1", "s3cret")
    assert _mode(p.parent) == 0o700 and _mode(p) == 0o600
    assert tunnel.read_token_file("k1") == "s3cret"
    assert tunnel.read_token_file("absent") is None


def test_token_dir_loose_mode_is_tightened(tmp_path):
    tunnel._write_token_file("k1", "t")
    d = tunnel.token_file_path("k1").parent
    d.chmod(0o777)
    assert tunnel.read_token_file("k1") == "t"
    assert _mode(d) == 0o700


def test_token_dir_symlink_refused(tmp_path):
    d = tunnel.token_file_path("k1").parent
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d.symlink_to(elsewhere)
    with pytest.raises(PermissionError):
        tunnel._write_token_file("k1", "t")
    assert list(elsewhere.iterdir()) == []


def test_token_dir_foreign_owner_refused(tmp_path, monkeypatch):
    tunnel._write_token_file("k1", "t")
    fixed = tunnel.token_file_path("k1")
    monkeypatch.setattr(tunnel, "token_file_path", lambda key: fixed.parent / f"{key}.token")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(fixed).st_uid + 1)
    with pytest.raises(PermissionError, match="owned"):
        tunnel._write_token_file("k2", "t")
    with pytest.raises(PermissionError, match="owned"):
        tunnel.read_token_file("k1")


def test_token_file_symlink_and_loose_mode_refused(tmp_path):
    p = tunnel._write_token_file("k1", "t")
    target = tmp_path / "real"
    target.write_text("stolen")
    link = p.parent / "k2.token"
    link.symlink_to(target)
    with pytest.raises(PermissionError):
        tunnel.read_token_file("k2")
    with pytest.raises(OSError):  # O_NOFOLLOW on write too
        tunnel._write_token_file("k2", "x")
    assert target.read_text() == "stolen"
    p.chmod(0o644)
    with pytest.raises(PermissionError, match="private"):
        tunnel.read_token_file("k1")


def test_cli_token_resolution(tmp_path, monkeypatch, capsys):
    f = tmp_path / "tok"
    f.write_text("from-file\n")
    monkeypatch.setenv("NEXUS_LITE_TOKEN", "from-env")
    assert tunnel.resolve_cli_token("flag", str(f)) == "from-file"
    assert capsys.readouterr().err == ""
    assert tunnel.resolve_cli_token("flag", None) == "flag"
    assert "deprecated" in capsys.readouterr().err
    assert tunnel.resolve_cli_token(None, None) == "from-env"
    assert capsys.readouterr().err == ""
    monkeypatch.delenv("NEXUS_LITE_TOKEN")
    assert tunnel.resolve_cli_token(None, None) is None
    f.write_text("  \n")
    with pytest.raises(ValueError):
        tunnel.resolve_cli_token(None, str(f))


def test_cli_exec_token_file_errors_and_env(tmp_path, monkeypatch, capsys):
    assert tunnel.main(["exec", "true", "--token-file", str(tmp_path / "missing")]) == 2
    assert "error" in capsys.readouterr().err
    monkeypatch.setenv("NEXUS_LITE_TOKEN", "tok")
    seen = {}

    class FakeClient:
        def __init__(self, url, token):
            seen.update(url=url, token=token)

        def exec(self, cmd):
            return {"stdout": "", "stderr": "", "rc": 0}

    monkeypatch.setattr(tunnel, "ColabTunnelClient", FakeClient)
    assert tunnel.main(["exec", "true", "--url", "http://127.0.0.1:1"]) == 0
    assert seen == {"url": "http://127.0.0.1:1", "token": "tok"}
    assert tunnel.main(["exec", "true", "--url", "http://127.0.0.1:1", "--token", "flag"]) == 0
    assert seen["token"] == "flag" and "deprecated" in capsys.readouterr().err


# ---- colab/pc.py ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel,norm", [("a/b.txt", "a/b.txt"), ("a/../b", "b"), ("./x//y", "x/y")])
def test_validate_rel_accepts_and_normalises(rel, norm):
    assert pcmod._validate_rel(rel) == norm


@pytest.mark.parametrize("rel", ["", "..", "../x", "a/../../x", "/etc/passwd", "a/b/../../..", "x\x00y"])
def test_validate_rel_rejects_traversal(rel):
    with pytest.raises(ValueError):
        pcmod._validate_rel(rel)


@pytest.mark.parametrize("agent", ["", ".", "..", "a/b", "a b", "../x"])
def test_validate_agent_rejects(agent):
    with pytest.raises(ValueError):
        pcmod._validate_agent(agent)


def test_pc_token_roundtrip_and_meta_never_holds_token(ws, monkeypatch):
    monkeypatch.delenv("NEXUS_COLAB_TOKEN", raising=False)
    pc = pcmod.bind_pc("alice", url="http://127.0.0.1:9", token="pc-s3cret", bootstrap=False)
    assert pc.client.token == "pc-s3cret"
    assert pcmod._read_token("alice") == "pc-s3cret"
    assert pcmod.get_pc("alice").client.token == "pc-s3cret"
    meta = ws / "pcs" / "alice.json"
    assert "pc-s3cret" not in meta.read_text()
    assert _mode(meta) == 0o600 and _mode(ws / "pcs") == 0o700
    tf = tunnel.token_file_path("pc-alice")
    assert _mode(tf) == 0o600
    assert pcmod.unbind_pc("alice") is True
    assert not tf.exists() and not meta.exists()


def test_pc_read_token_env_fallback(monkeypatch):
    monkeypatch.setenv("NEXUS_COLAB_TOKEN", " env-tok ")
    assert pcmod._read_token("nobody") == "env-tok"


# ---- 3. cursors ----------------------------------------------------------------------------

def _inbox_specs():
    return [{"task_id": "A", "prompt": "pa", "inject_bus_inbox": True},
            {"task_id": "B", "prompt": "pb", "inject_bus_inbox": True}]


def test_turn_cursor_not_acked_when_dispatch_fails(monkeypatch):
    mtm.bus_send("A", "for-a")
    mtm.bus_send("B", "for-b")

    def wave(specs, caller, timeout, verbose, on_dispatch=None):
        raise RuntimeError("dispatch failed")  # before any spec was dispatched

    monkeypatch.setattr(nl, "_swarm_wave", wave)
    with pytest.raises(RuntimeError):
        nl.run_swarm(_inbox_specs())
    assert len(mtm.bus_read("A", unacked=True, consumer="turn")) == 1
    assert len(mtm.bus_read("B", unacked=True, consumer="turn")) == 1


def test_turn_cursor_acked_only_for_dispatched_specs(monkeypatch):
    mtm.bus_send("A", "for-a")
    mtm.bus_send("B", "for-b")

    def wave(specs, caller, timeout, verbose, on_dispatch=None):
        on_dispatch(0)  # first spec dispatched, second one blows up
        raise RuntimeError("second dispatch failed")

    monkeypatch.setattr(nl, "_swarm_wave", wave)
    with pytest.raises(RuntimeError):
        nl.run_swarm(_inbox_specs())
    assert mtm.bus_read("A", unacked=True, consumer="turn") == []
    assert len(mtm.bus_read("B", unacked=True, consumer="turn")) == 1


def test_prune_keeps_all_messages_for_registered_consumer_without_cursor():
    mtm.bus_register("sess")  # live, has never acked anything
    seq = mtm.bus_send("ALL", "broadcast")["seq"]
    mtm.bus_send("ALL", "newer")
    mtm.bus_ack("other", seq)  # someone else read it
    assert mtm.bus_prune(0) == 0
    mtm.bus_ack("sess", seq)
    assert mtm.bus_prune(0) == 1
    assert [m["summary"] for m in mtm.bus_read()] == ["newer"]


def test_prune_ignores_unregistered_session_without_cursor():
    mtm.bus_register("gone")
    mtm.bus_unregister("gone")
    seq = mtm.bus_send("ALL", "broadcast")["seq"]
    mtm.bus_send("ALL", "newer")
    mtm.bus_ack("other", seq)
    assert mtm.bus_prune(0) == 1


def test_prune_never_acked_direct_target_kept():
    mtm.bus_send("ghost", "unread")
    mtm.bus_send("ghost", "newer")
    assert mtm.bus_prune(0) == 0


def test_register_resend_uses_cursors_in_use():
    s1 = mtm.bus_send("w", "a")["seq"]
    m2 = mtm.bus_send("w", "b")
    mtm.bus_ack("w", s1, consumer="watch")  # only a named consumer was ever used
    assert mtm.bus_register("w")["redelivered"] == [m2["id"]]


def test_register_resend_everything_without_cursor_and_lowest_cursor_wins():
    m1 = mtm.bus_send("w", "a")
    m2 = mtm.bus_send("w", "b")
    assert mtm.bus_register("w")["redelivered"] == [m1["id"], m2["id"]]
    mtm.bus_unregister("w")
    mtm.bus_ack("w", m2["seq"], consumer="pty")
    mtm.bus_ack("w", m1["seq"], consumer="turn")  # turn still has m2 unread
    assert mtm.bus_register("w")["redelivered"] == [m2["id"]]


def _stub_delegate(monkeypatch, statuses: dict, calls: list):
    """delegate() stand-in: records the call, inserts a task row with the given terminal status
    ('running' = never finishes)."""
    def fake(**kw):
        calls.append(kw["task_id"])
        with _db.connect() as c:
            c.execute("INSERT OR REPLACE INTO tasks (id, caller, agent_cmd, status, rc, created_at, completed_at,"
                      " artifact_dir) VALUES (?, 'o', 'x', ?, 0, ?, ?, ?)",
                      (kw["task_id"], statuses[kw["task_id"]], time.time(), time.time(), str(_db.get_workspace())))
        return {"task_id": kw["task_id"]}

    monkeypatch.setattr(nl, "delegate", fake)
    monkeypatch.setattr(nl, "reap_stale_tasks", lambda: None)


def test_subwave_timeout_marks_task_and_skips_dependents(monkeypatch):
    calls: list = []
    _stub_delegate(monkeypatch, {"A": "running", "C": "completed", "B": "completed", "D": "completed"}, calls)
    specs = [
        {"task_id": "A", "cmd": "x", "prompt": "pa"},
        {"task_id": "C", "cmd": "x", "prompt": "pc"},
        {"task_id": "B", "cmd": "x", "prompt": "pb", "depends_on": ["A"]},
        {"task_id": "D", "cmd": "x", "prompt": "pd", "depends_on": ["B"], "bus_targets": ["sink"]},
    ]
    res = nl.run_swarm(specs, timeout=0.4)
    by = {r["task_id"]: r for r in res}
    assert [r["task_id"] for r in res] == ["A", "C", "B", "D"]
    assert by["A"]["status"] == "timeout" and by["C"]["status"] == "completed"
    assert by["B"]["status"] == "skipped" and by["B"]["skipped_because"] == ["A"]
    assert by["D"]["status"] == "skipped"  # transitive: its dependency was skipped
    assert calls == ["A", "C"]  # dependents never dispatched
    with _db.connect() as c:
        assert c.execute("SELECT status FROM tasks WHERE id='A'").fetchone()["status"] == "timeout"
        assert c.execute("SELECT COUNT(*) FROM messages WHERE target='sink'").fetchone()[0] == 0
    assert all(r is not None for r in res)


def test_subwave_failed_dependency_still_runs_dependents(monkeypatch):
    calls: list = []
    _stub_delegate(monkeypatch, {"A": "failed", "B": "completed"}, calls)
    res = nl.run_swarm([{"task_id": "A", "cmd": "x"}, {"task_id": "B", "cmd": "x", "depends_on": ["A"]}], timeout=5)
    assert calls == ["A", "B"] and [r["status"] for r in res] == ["failed", "completed"]


def test_single_wave_timeout_status():
    """Legacy one-wave path: a task that never finishes is reported as timeout, not 'running'."""
    _db.init()
    with _db.connect() as c:
        c.execute("INSERT INTO tasks (id, caller, agent_cmd, status, created_at, artifact_dir)"
                  " VALUES ('slow', 'o', 'x', 'running', ?, ?)", (time.time(), str(_db.get_workspace())))
    orig = nl.delegate
    nl.delegate = lambda **kw: {"task_id": kw["task_id"]}
    nl.reap_stale_tasks, saved = (lambda: None), nl.reap_stale_tasks
    try:
        res = nl._swarm_wave([{"task_id": "slow", "cmd": "x", "prompt": "p"}], "o", 0.3, False)
    finally:
        nl.delegate, nl.reap_stale_tasks = orig, saved
    assert res[0]["status"] == "timeout"


# ---- shlex-consistent command parsing ------------------------------------------------------

@pytest.mark.parametrize("cmd,name", [
    ('"/opt/my dir/bin/ollama" launch x', "ollama"),
    ("'/opt/my dir/claude' --flag", "claude"),
    ("python script.py", "python"),
    (["/usr/bin/agy", "--x"], "agy"),
    ("", ""),
    ('"unterminated', '"unterminated'),  # unparseable: falls back to whitespace split
])
def test_argv0_matches_shlex(cmd, name):
    assert nl._argv0(cmd) == name


def test_leaf_guard_sees_quoted_executable(monkeypatch):
    monkeypatch.setenv("NEXUS_DELEGATED_ROLE", "worker")
    with pytest.raises(PermissionError):
        nl.delegate('"/opt/my dir/nexus" mini x', "p", task_id="g-1")


def test_completion_detail_off_puts_status_only_on_the_bus(ws, tmp_path, typed):
    """A caller whose team must not see each other's output (e.g. the research harness) asks for the pointer-only
    completion message without PHI mode: the worker's output stays in its artifact directory."""
    res = nl.delegate(_agent(tmp_path, SECRET), "p", task_id="s-ok", caller="harness", completion_detail=False)
    assert res["status"] == "completed"
    with _db.connect() as c:
        row = c.execute("SELECT content FROM messages WHERE sender='s-ok'").fetchone()
    assert row["content"] == "TASK_COMPLETE s-ok: completed"
    assert SECRET not in _bus_dump(ws)


def test_completion_detail_defaults_to_the_first_output_line(ws, tmp_path, typed):
    nl.delegate(_agent(tmp_path, SECRET), "p", task_id="s-def", caller="main")
    with _db.connect() as c:
        row = c.execute("SELECT content FROM messages WHERE sender='s-def'").fetchone()
    assert SECRET in row["content"]
