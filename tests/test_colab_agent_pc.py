"""Agent PC (World <-> dedicated Colab workstation) tests. Hermetic: loopback tunnel, tmp dirs only."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import nexus_lite as nl
from nexus_lite.colab import pc as pcmod
from nexus_lite.colab import tunnel


@pytest.fixture()
def world(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    socks = tmp_path / "s"
    socks.mkdir()
    roots = tmp_path / "pcroot"
    roots.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(socks))
    monkeypatch.setenv("NEXUS_LITE_PC_ROOT", str(roots))
    monkeypatch.delenv("NEXUS_LITE_TUNNEL_TOKEN", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    nl.init(ws)
    info = tunnel.start_tunnel(port=0)
    yield {"ws": ws, "roots": roots, "info": info, "tmp": tmp_path}
    tunnel.stop_tunnel()


def _bind(world, agent, **kw):
    return pcmod.bind_pc(agent, url=world["info"]["local_url"], token=world["info"]["token"],
                         remote_home=str(world["roots"] / agent), **kw)


def _post(world, path, payload, token=True):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {world['info']['token']}"
    r = urllib.request.Request(world["info"]["local_url"] + path, data=json.dumps(payload).encode(),
                               headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        e.close()
        return e.code


def test_agent_name_validation(world):
    for bad in ("", ".", "..", "a/b", "a b"):
        with pytest.raises(ValueError):
            pcmod.bind_pc(bad, url="http://127.0.0.1:1", bootstrap=False)


def test_registry_token_never_in_metadata(world):
    pc = _bind(world, "physicist", bootstrap=False)
    meta_file = world["ws"] / "pcs" / "physicist.json"
    meta = json.loads(meta_file.read_text())
    assert world["info"]["token"] not in meta_file.read_text()
    assert {"agent", "url", "remote_home", "remote_work", "remote_nexus", "bundle_sha", "created_at",
            "last_seen"} <= set(meta)
    tf = tunnel.token_file_path("pc-physicist")
    assert tf.read_text() == world["info"]["token"] and (tf.stat().st_mode & 0o777) == 0o600
    assert tunnel.token_file_path(8765).name == "tunnel-8765.token"
    assert [m["agent"] for m in pcmod.list_pcs()] == ["physicist"]
    assert pcmod.get_pc("physicist").client.token == world["info"]["token"]
    assert pcmod.unbind_pc("physicist") is True and pcmod.unbind_pc("physicist") is False
    assert not tf.exists()
    assert pc.agent == "physicist"


def test_runtime_cache_reuse(world):
    pc = _bind(world, "physicist", bootstrap=False)
    first = pc.ensure_runtime()
    assert first["reused"] is False and first["uploaded_bytes"] > 0
    calls = []
    real = pc.client.fs_write_bytes
    pc.client.fs_write_bytes = lambda *a, **k: calls.append(a) or real(*a, **k)
    second = pc.ensure_runtime()
    assert second["reused"] is True and second["bundle_sha"] == first["bundle_sha"] and not calls
    assert json.loads((world["ws"] / "pcs" / "physicist.json").read_text())["bundle_sha"] == first["bundle_sha"]
    assert pc.ensure_runtime(force=True)["reused"] is False


def test_multi_agent_isolation(world):
    a = _bind(world, "physicist")
    b = _bind(world, "biostat")
    a.run(cmd="echo A > note.txt")
    b.run(cmd="echo B > note.txt")
    # persistent + independent work dirs across turns
    assert a.run(cmd="cat note.txt")["stdout"].strip() == "A"
    assert b.run(cmd="cat note.txt")["stdout"].strip() == "B"
    # independent private .nexus (STM / wiki)
    ra = a.run(python="from nexus_lite.memory import ltm\nltm.wiki_write('secret','phys only')\n"
                      "RESULT = str(WORKSPACE)")
    assert ra["ok"], ra
    assert ra["result"] == a.remote_nexus
    rb = b.run(python="from nexus_lite.memory import ltm\n"
                      "try:\n    ltm.wiki_read('secret'); RESULT='leak'\nexcept FileNotFoundError:\n    RESULT='isolated'")
    assert rb["result"] == "isolated"
    assert not (world["ws"] / "wiki" / "secret.md").exists()  # not in the World either
    # isolated PTYs
    a.pty_spawn("cat", "shell")
    b.pty_spawn("cat", "shell")
    try:
        assert [s["name"] for s in a.pty_list()] == ["physicist-shell"]
        assert [s["name"] for s in b.pty_list()] == ["biostat-shell"]
        a.pty_send("shell", "hello-from-a")
        time.sleep(0.6)
        assert "hello-from-a" in a.pty_read("shell")
        assert "hello-from-a" not in b.pty_read("shell")
    finally:
        a.pty_kill("shell")
        b.pty_kill("shell")
    assert a.pty_list() == []


def test_run_python_globals_and_world_stm(world):
    pc = _bind(world, "physicist")
    from nexus_lite.memory import stm as host_stm

    host_stm.stm_write(None, kind="decision", status="running", summary="use dose grid 2mm")
    res = pc.checkout(stm=True, bus_unread=False)
    assert res["stm_synced"]
    out = pc.run(python="RESULT = {'has_stm': bool(WORLD_STM), 'cwd': str(WORK_DIR), 'ws': str(WORKSPACE)}",
                 collect=[".world_stm.json"])
    assert out["ok"] and out["result"]["has_stm"] and out["result"]["cwd"] == pc.remote_work
    assert out["artifacts"][".world_stm.json"]["exists"]
    # synced decision visible in PC's private STM
    r = pc.run(python="from nexus_lite.memory import stm\n"
                      "RESULT = [n['summary'] for n in stm.stm_list(kind='decision')]")
    assert "use dose grid 2mm" in r["result"]
    bad = pc.run(python="raise RuntimeError('boom')")
    assert not bad["ok"] and bad["status"] == "failed" and "boom" in bad["stderr"]
    lite = pc.run(lite_argv=["--help"])
    assert lite["rc"] == 0


def test_binary_roundtrip_checkout_run_publish(world, monkeypatch):
    pc = _bind(world, "physicist")
    blob = os.urandom(1_300_000)
    src = world["tmp"] / "blob.bin"
    src.write_bytes(blob)
    pc.checkout(stm=False, bus_unread=False, files={"in/blob.bin": src, "in/tiny.png": b"\x89PNG\r\n\x00\xff"})
    r = pc.run(python="import hashlib\nd = (WORK_DIR/'in'/'blob.bin').read_bytes()\n"
                      "(WORK_DIR/'out').mkdir(exist_ok=True)\n(WORK_DIR/'out'/'copy.npz').write_bytes(d)\n"
                      "RESULT = hashlib.sha256(d).hexdigest()")
    assert r["result"] == hashlib.sha256(blob).hexdigest()
    pub = pc.publish(summary="done physics", artifacts=["out/copy.npz", "in/tiny.png"], task_id="t-bin1",
                     notify="ALL")
    got = world["ws"] / "artifacts" / "t-bin1" / "out" / "copy.npz"
    assert hashlib.sha256(got.read_bytes()).hexdigest() == hashlib.sha256(blob).hexdigest()
    assert (world["ws"] / "artifacts" / "t-bin1" / "in" / "tiny.png").read_bytes() == b"\x89PNG\r\n\x00\xff"
    assert pub["ok"] and pub["stm_node"]["task_id"] == "t-bin1" and pub["bus_message"]["sender"] == "physicist"
    import json as _json
    from nexus_lite import cas
    mf = _json.loads((world["ws"] / "artifacts" / "t-bin1" / "ARTIFACT_MANIFEST.json").read_text())
    assert mf["files"]["out/copy.npz"]["sha256"] == hashlib.sha256(blob).hexdigest() and cas.has(mf["files"]["out/copy.npz"]["sha256"])
    assert mf["provenance"]["task_id"] == "t-bin1" and cas.ref_get("tasks/t-bin1") == pub["manifest_sha256"]
    assert pub["skipped"] == [] and pub["manifest_sha256"]
    # second publish: host CAS already has both hashes -> no download, no rehash
    calls = []
    orig = pc.client.fs_download
    monkeypatch.setattr(pc.client, "fs_download", lambda *a, **k: calls.append(a) or orig(*a, **k))
    pub2 = pc.publish(summary="again", artifacts=["out/copy.npz", "in/tiny.png"], task_id="t-bin2", notify=None)
    assert calls == [] and sorted(pub2["skipped"]) == ["in/tiny.png", "out/copy.npz"]
    assert (world["ws"] / "artifacts" / "t-bin2" / "out" / "copy.npz").read_bytes() == blob
    with pytest.raises(ValueError):
        pc.publish(summary="x", artifacts=["../evil"])


def test_wiki_and_bus_sync(world):
    from nexus_lite.memory import ltm, mtm

    pc = _bind(world, "biostat")
    ltm.wiki_write("protocol", "# protocol v1")
    mtm.bus_send("biostat", "please check cohort", sender="orchestrator")
    mtm.bus_send("other", "not for you", sender="orchestrator")
    res = pc.checkout(stm=False, wiki_slugs=["protocol"], bus_unread=True)
    assert res["wiki_synced"] == ["protocol"] and res["bus_delivered"] >= 1
    r = pc.run(python="from nexus_lite.memory import ltm, mtm\n"
                      "RESULT = [ltm.wiki_read('protocol'), [m['content'].strip() for m in mtm.bus_read(target='biostat')]]")
    assert r["result"][0] == "# protocol v1" and "please check cohort" in r["result"][1]
    assert not any("not for you" in c for c in r["result"][1])
    assert pc.checkout(stm=False, bus_unread=True)["bus_delivered"] == 0  # already acked
    pc.run(python="from nexus_lite.memory import ltm\nltm.wiki_write('findings', 'biostat result')")
    out = pc.publish(summary="stats done", wiki_pages=["findings"], notify=None)
    assert out["wiki_written"] == ["findings"] and ltm.wiki_read("findings") == "biostat result"
    assert out["bus_message"] is None
    assert pc.publish(summary="s", wiki_pages={"direct": "body"}, notify=None)["wiki_written"] == ["direct"]


def test_bg_job_lifecycle(world):
    pc = _bind(world, "physicist")
    t0 = time.time()
    sub = pc.run(cmd="sleep 1.5; echo finished", mode="bg", job_id="slow1")
    assert time.time() - t0 < 2 + 3  # returns without waiting for the job (bootstrap excluded by margin)
    assert sub["status"] == "submitted" and sub["pid"] and sub["job_id"] == "slow1"
    assert pc.job_status("slow1")["status"] == "running"
    done = pc.job_wait("slow1", timeout=30)
    assert done["status"] == "completed" and "finished" in done["stdout"]
    fail = pc.run(cmd="exit 3", mode="bg", job_id="bad1")
    assert pc.job_wait("bad1", timeout=30)["status"] == "failed" and fail["ok"]
    pc.run(cmd="sleep 30", mode="bg", job_id="long1")
    with pytest.raises(TimeoutError):
        pc.job_wait("long1", poll_interval=0.1, timeout=0.5)
    pc.client.exec(cmd=f"kill $(cat {pc.remote_home}/jobs/long1/runner.pid)")
    assert pc.job_status("nope")["status"] == "failed"


def _cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = tunnel.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_cli_flow(world):
    home = str(world["roots"] / "cli")
    rc, out, _ = _cli(["pc", "bind", "cliagent", "--url", world["info"]["local_url"],
                       "--token", world["info"]["token"], "--remote-home", home])
    assert rc == 0 and json.loads(out)["agent"] == "cliagent"
    assert world["info"]["token"] not in (world["ws"] / "pcs" / "cliagent.json").read_text()
    assert json.loads(_cli(["pc", "ls"])[1])["pcs"][0]["agent"] == "cliagent"
    rc, out, _ = _cli(["pc", "run", "cliagent", "--bg", "--job-id", "clijob", "--cmd", "echo hi"])
    assert rc == 0 and json.loads(out)["status"] == "submitted"
    rc, out, _ = _cli(["pc", "poll", "cliagent", "clijob", "--wait", "--timeout", "30"])
    assert rc == 0 and json.loads(out)["status"] == "completed"
    rc, out, _ = _cli(["pc", "run", "cliagent", "--python", "RESULT = 40 + 2", "--collect", "nothing.txt"])
    assert rc == 0 and json.loads(out)["result"] == 42
    rc, out, _ = _cli(["pc", "run", "cliagent", "--cmd", "exit 2"])
    assert rc == 1
    rc, out, _ = _cli(["pc", "run", "cliagent", "--lite", "--help"])
    assert rc == 0
    rc, out, _ = _cli(["pc", "checkout", "cliagent", "--no-bus"])
    assert rc == 0 and json.loads(out)["stm_synced"] is True
    rc, out, _ = _cli(["pc", "publish", "cliagent", "--summary", "cli done", "--task-id", "t-cli"])
    assert rc == 0 and json.loads(out)["task_id"] == "t-cli"
    rc, out, _ = _cli(["pc", "pty", "cliagent", "list"])
    assert rc == 0 and json.loads(out)["sessions"] == []
    assert _cli(["pc", "unbind", "cliagent"])[0] == 0
    assert _cli(["pc", "status", "cliagent"])[0] == 1  # unbound -> failure


def test_phi_gate_fails_closed(world, monkeypatch):
    pc = _bind(world, "physicist")
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    for call in (
        lambda: pcmod.bind_pc("x", url="http://127.0.0.1:1"),
        lambda: pcmod.get_pc("physicist"), pcmod.list_pcs, lambda: pcmod.unbind_pc("physicist"),
        pc.ensure_runtime, lambda: pc.run(cmd="true"), lambda: pc.job_status("j"),
        lambda: pc.job_wait("j"), lambda: pc.pty_spawn("cat"), lambda: pc.pty_list(),
        lambda: pc.pty_read("shell"), lambda: pc.checkout(), lambda: pc.publish(summary="s"),
    ):
        with pytest.raises(PermissionError):
            call()
    for argv in (["pc", "ls"], ["pc", "run", "physicist", "--cmd", "true"],
                 ["pc", "bind", "z", "--url", "http://127.0.0.1:1", "--no-bootstrap"]):
        rc, out, err = _cli(argv)
        assert rc == 78 and out == "" and "PHI gate" in err


def test_path_confinement_403(world):
    secret = world["info"]["token"]
    root = world["roots"]
    for body in ({"action": "stat", "path": "/etc/passwd"},
                 {"action": "stat", "path": str(root / ".." / "ws" / "x")[:-1] + "/../../../etc/passwd"},
                 {"action": "read_chunk", "path": "../../../../etc/passwd"},
                 {"action": "write_chunk", "path": "/etc/nexus-evil", "data_zb64": "eJwDAAAAAAE="},
                 {"action": "stat", "path": ""}):
        assert _post(world, "/fs", body) == 403, body
    assert _post(world, "/exec", {"cmd": "true", "cwd": "/etc"}) == 403
    assert _post(world, "/exec", {"cmd": "true", "cwd": str(root / ".." / "..")}) == 403
    assert _post(world, "/exec", {"cmd": "true", "cwd": str(root)}) == 200
    assert _post(world, "/fs", {"action": "stat", "path": str(root)}) == 200
    assert _post(world, "/fs", {"action": "stat", "path": "x"}, token=False) == 401
    assert secret  # token only ever travels in the Authorization header


def test_fs_roundtrip_chunks_and_exec_env(world):
    c = tunnel.ColabTunnelClient(world["info"]["local_url"], world["info"]["token"])
    data = os.urandom(300_000)
    p = str(world["roots"] / "d" / "f.bin")
    w = c.fs_write_bytes(p, data, chunk_size=70_000)
    assert w["sha256"] == hashlib.sha256(data).hexdigest()
    st = c.fs_stat(p)
    assert st["exists"] and st["size"] == len(data) and st["sha256"] == w["sha256"]
    assert c.fs_read_bytes(p, chunk_size=64_000) == data
    assert not c.fs_stat(p + ".missing")["exists"]
    assert c.fs_write_bytes(p, b"")["size"] == 0 and c.fs_read_bytes(p) == b""
    r = c.exec(cmd="echo $PC_X; pwd", cwd=str(world["roots"] / "newdir"), env={"PC_X": "v1"}, mkdir_cwd=True)
    assert r["stdout"].split() == ["v1", str((world["roots"] / "newdir").resolve())]
    with pytest.raises(urllib.error.HTTPError) as ei:
        c.fs_read_bytes(p + ".missing")
    assert ei.value.code == 404
    ei.value.close()


def _main_cli(argv):
    from nexus_lite import cli
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_unified_cli_dispatch(world):
    home = str(world["roots"] / "uni")
    rc, out, _ = _main_cli(["pc", "bind", "uni", "--url", world["info"]["local_url"],
                            "--token", world["info"]["token"], "--remote-home", home])
    assert rc == 0 and json.loads(out)["agent"] == "uni"
    assert json.loads(_main_cli(["pc", "ls"])[1])["pcs"][0]["agent"] == "uni"
    assert json.loads(_main_cli(["colab", "pc", "ls"])[1])["pcs"][0]["agent"] == "uni"
    rc, out, _ = _main_cli(["pc", "run", "uni", "--python", "RESULT = 1 + 1"])
    assert rc == 0 and json.loads(out)["result"] == 2
    rc, out, _ = _main_cli(["pc", "checkout", "uni", "--no-bus"])
    assert rc == 0 and json.loads(out)["stm_synced"] is True
    rc, out, _ = _main_cli(["pc", "publish", "uni", "--summary", "s", "--task-id", "t-uni"])
    assert rc == 0 and json.loads(out)["task_id"] == "t-uni"


def test_unified_cli_phi_fails_closed(world, monkeypatch):
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    rc, out, err = _main_cli(["pc", "ls"])
    assert rc == 78 and out == "" and "PHI gate" in err


# ---------------- `colab` CLI backend (no tunnel) ----------------
_FAKE_COLAB = r'''#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
state = os.environ["FAKE_COLAB_STATE"]
log = state + ".log"
a = sys.argv[1:]
open(log, "a").write(json.dumps(a) + "\n")
sess = json.load(open(state)) if os.path.exists(state) else {}
def opt(flag, default=None):
    return a[a.index(flag) + 1] if flag in a else default
cmd = a[0]
if cmd == "sessions":
    for i, (n, v) in enumerate(sess.items()):
        print(f"[{n}] {v['ep']} | Hardware: {v['hw']} | Shape: Standard | Variant: DEFAULT")
elif cmd == "new":
    n = opt("-s", "auto%d" % len(sess))
    sess[n] = {"id": "%06x" % (len(sess) + 0xabc000), "ep": "m-s-fake-" + n, "hw": opt("--gpu", "CPU")}
    json.dump(sess, open(state, "w"))
    print("[colab] created", n)
elif cmd == "stop":
    s = opt("-s")
    if s not in sess:
        sys.exit(1)
    del sess[s]
    json.dump(sess, open(state, "w"))
elif cmd in ("exec", "upload", "download"):
    s = opt("-s")
    if s not in sess:
        print("no such session", file=sys.stderr); sys.exit(1)
    if cmd == "exec":
        p = subprocess.run([sys.executable, opt("-f")], capture_output=True, text=True)
        sys.stdout.write(p.stdout); sys.stderr.write(p.stderr)
    else:
        pos = [x for i, x in enumerate(a[1:], 1) if x != "-s" and a[i - 1] != "-s"]
        src, dst = (pos[0], pos[1])
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copyfile(src, dst)
else:
    sys.exit(2)
'''


@pytest.fixture()
def fakecolab(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    (tmp_path / "socks").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    bin_ = tmp_path / "fake-colab"
    bin_.write_text(_FAKE_COLAB)
    bin_.chmod(0o755)
    state = tmp_path / "colab-state.json"
    monkeypatch.setenv("NEXUS_COLAB_BIN", str(bin_))
    monkeypatch.setenv("FAKE_COLAB_STATE", str(state))
    nl.init(ws)

    def calls():
        lg = tmp_path / "colab-state.json.log"
        return [json.loads(x) for x in lg.read_text().splitlines()] if lg.exists() else []

    return {"state": state, "calls": calls, "tmp": tmp_path, "ws": ws}


def test_colab_cli_new_sessions_stop(fakecolab):
    rc, out, _ = _main_cli(["colab", "new", "-s", "alpha", "--gpu", "T4", "--high-mem"])
    assert rc == 0 and json.loads(out)["session"]
    assert ["new", "-s", "alpha", "--gpu", "T4", "--high-mem"] in fakecolab["calls"]()
    rc, out, _ = _main_cli(["colab", "sessions"])
    sess = json.loads(out)["sessions"]
    assert rc == 0 and sess[0]["endpoint"] == "m-s-fake-alpha" and sess[0]["hardware"] == "T4"
    assert sess[0]["shape"] == "Standard" and sess[0]["variant"] == "DEFAULT"
    assert json.loads(_main_cli(["colab", "ls"])[1])["sessions"] == sess
    rc, out, _ = _main_cli(["colab", "stop", "-s", "alpha"])
    assert rc == 0 and json.loads(out)["stopped"] is True
    assert json.loads(_main_cli(["colab", "sessions"])[1])["sessions"] == []


def test_colab_cli_exec_session_no_env_flag(fakecolab):
    _main_cli(["colab", "new", "-s", "alpha"])
    rc, out, _ = _main_cli(["colab", "exec", "echo hi", "-s", "alpha"])
    assert rc == 0 and out.strip() == "hi"
    cl = tunnel.ColabCliClient("alpha")
    res = cl.exec(python="import os; print(os.environ['SECRET_X'])", env={"SECRET_X": "s3cret"})
    assert res["rc"] == 0 and res["stdout"].strip() == "s3cret"
    assert all("--env" not in c and "s3cret" not in " ".join(c) for c in fakecolab["calls"]())
    assert cl.exec(cmd="exit 3")["rc"] == 3
    assert cl.exec(python="import time; time.sleep(5)", timeout=0.5)["timed_out"] is True


def test_bind_without_url_autocreates_and_full_flow(fakecolab):
    home = str(fakecolab["tmp"] / "pchome")
    pc = pcmod.bind_pc("physicist", remote_home=home)
    assert isinstance(pc.client, tunnel.ColabCliClient) and pc.client.session == "nx-physicist"
    assert ["new", "-s", "nx-physicist"] in fakecolab["calls"]()
    meta = json.loads((fakecolab["ws"] / "pcs" / "physicist.json").read_text())
    assert meta["colab_session"] == "nx-physicist" and not meta["url"]
    pc = pcmod.get_pc("physicist")
    assert pc.checkout(stm=True, bus_unread=False)["stm_synced"]
    fg = pc.run(python="RESULT = {'v': 42}\nopen('out.txt', 'w').write('artifact')", collect=["out.txt"])
    assert fg["ok"] and fg["result"] == {"v": 42} and fg["artifacts"]["out.txt"]["text"] == "artifact"
    bg = pc.run(cmd="echo bg-done", mode="bg", job_id="bgjob")
    assert bg["status"] == "submitted"
    done = pc.job_wait("bgjob", timeout=30)
    assert done["ok"] and "bg-done" in done["stdout"]
    pub = pc.publish(summary="done", artifacts=["out.txt"], task_id="t-colab", notify=None)
    assert (fakecolab["ws"] / "artifacts" / "t-colab" / "out.txt").read_text() == "artifact"
    assert pub["ok"]
    assert pc.client.health()["colab_session"] == "nx-physicist"
    assert pcmod.unbind_pc("physicist", stop_remote=True) is True
    assert ["stop", "-s", "nx-physicist"] in fakecolab["calls"]()
    assert json.loads(_main_cli(["colab", "sessions"])[1])["sessions"] == []


def test_bind_existing_session_does_not_call_new(fakecolab):
    _main_cli(["colab", "new", "-s", "shared"])
    n_new = sum(c[0] == "new" for c in fakecolab["calls"]())
    pc = pcmod.bind_pc("ana", colab_session="shared", remote_home=str(fakecolab["tmp"] / "ana"), bootstrap=False)
    assert pc.client.session == "shared"
    assert sum(c[0] == "new" for c in fakecolab["calls"]()) == n_new
    assert pcmod.unbind_pc("ana") is True  # no --stop-remote: session stays alive
    assert not any(c[:1] == ["stop"] for c in fakecolab["calls"]())
    rc, out, _ = _main_cli(["pc", "bind", "ana2", "--session", "shared", "--no-bootstrap",
                            "--remote-home", str(fakecolab["tmp"] / "ana2")])
    assert rc == 0 and json.loads(out)["colab_session"] == "shared"


def test_compute_colab_session(fakecolab):
    _main_cli(["colab", "new", "-s", "alpha"])
    rc, out, _ = _main_cli(["compute", "colab", "--session", "alpha", "--python", "RESULT = 6 * 7",
                            "--timeout", "120"])
    assert rc == 0 and json.loads(out)["result"] == 42


def test_colab_cli_phi_fails_closed(fakecolab, monkeypatch):
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    with pytest.raises(PermissionError):
        tunnel.colab_new(session="x")
    with pytest.raises(PermissionError):
        tunnel.ColabCliClient("x").exec(cmd="true")
    with pytest.raises(PermissionError):
        pcmod.bind_pc("phi")
    rc, out, err = _main_cli(["colab", "new", "-s", "x"])
    assert rc == 78 and "PHI gate" in err
    assert fakecolab["calls"]() == []


def test_cli_exec_spec_never_in_wrapper_and_perms(fakecolab, monkeypatch):
    """P1/P2: env travels via `colab upload` to /dev/shm; wrapper .py holds no secret; umask 077."""
    _main_cli(["colab", "new", "-s", "alpha"])
    seen = []
    real = tunnel._colab_run

    def spy(args, *, timeout):
        if args[0] == "exec":
            seen.append(Path(args[args.index("-f") + 1]).read_text())
        return real(args, timeout=timeout)

    monkeypatch.setattr(tunnel, "_colab_run", spy)
    cl = tunnel.ColabCliClient("alpha")
    res = cl.exec(python="import os; print(os.environ['SECRET_X'])", env={"SECRET_X": "s3cret"})
    assert res["rc"] == 0 and res["stdout"].strip() == "s3cret"
    assert seen and all("s3cret" not in w and "SECRET_X" not in w for w in seen)
    ups = [c for c in fakecolab["calls"]() if c[0] == "upload"]
    assert ups and ups[0][-1].startswith("/dev/shm/nx-spec-")
    assert not Path(ups[0][-1]).exists()  # wrapper unlinked it


def test_tighten_colab_cli_perms(tmp_path, monkeypatch):
    home = tmp_path / "h"
    hist = home / ".config" / "colab-cli" / "history"
    hist.mkdir(parents=True)
    f = hist / "s.jsonl"
    f.write_text("x")
    f.chmod(0o644)
    (home / ".config" / "colab-cli" / "colab.log").write_text("l")
    monkeypatch.setenv("HOME", str(home))
    tunnel._tighten_colab_cli_perms()
    assert (f.stat().st_mode & 0o777) == 0o600
    assert ((home / ".config" / "colab-cli").stat().st_mode & 0o777) == 0o700
    assert (hist.stat().st_mode & 0o777) == 0o700


def test_pc_reduced_cli_roundtrips(fakecolab, monkeypatch):
    """P3: runtime reuse skips the probe; run() = 1 upload(spec) + 1 exec."""
    if not os.path.isdir("/dev/shm"):
        monkeypatch.setenv(tunnel.REMOTE_TMP_ENV, str(fakecolab["tmp"]))
    pcmod.bind_pc("physicist", remote_home=str(fakecolab["tmp"] / "pchome"))
    pc = pcmod.get_pc("physicist")
    pc.ensure_runtime()
    assert pcmod.get_pc("physicist").runtime_dir == pc.runtime_dir
    n0 = len(fakecolab["calls"]())
    assert pc.ensure_runtime()["reused"] is True
    assert len(fakecolab["calls"]()) == n0  # zero remote round-trips
    pc.run(python="RESULT = 1")
    new = fakecolab["calls"]()[n0:]
    assert [c[0] for c in new] == ["upload", "exec"]
