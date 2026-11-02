"""Secure Colab tunnel defaults and client test suite. Hermetic: loopback only, no network, mocked tunnel."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

import nexus_lite as nl
from nexus_lite.colab import tunnel


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    socks = tmp_path / "s"
    socks.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(socks))
    monkeypatch.delenv("NEXUS_LITE_TUNNEL_TOKEN", raising=False)
    monkeypatch.delenv("NEXUS_LITE_BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    nl.init(ws)
    yield ws
    tunnel.stop_tunnel()


def _req(url, *, method="GET", headers=None, body=None):
    r = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


def _post(info, path, payload, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return _req(info["local_url"] + path, method="POST", headers=h, body=json.dumps(payload).encode())


def test_header_only_token(env):
    info = tunnel.start_tunnel(port=0)
    u, t = info["local_url"], info["token"]
    assert _req(u + "/healthz")[0] == 401
    assert _req(f"{u}/healthz?token={t}")[0] == 401  # query string token not accepted
    assert _req(u + "/healthz", headers={"Authorization": f"Token {t}"})[0] == 401
    assert _req(u + "/healthz", headers={"Authorization": "Bearer wrong"})[0] == 401
    assert _req(u + "/healthz", headers={"Authorization": f"Bearer {t}"})[0] == 200
    assert t not in info["dashboard_url"]


@pytest.mark.parametrize("tok", ["", "   "])
def test_empty_token_refuses_to_start(tok):
    with pytest.raises(ValueError):
        tunnel.start_tunnel(port=0, token=tok)
    assert tunnel._SERVER is None


def test_empty_server_token_never_authenticates(env):
    info = tunnel.start_tunnel(port=0)
    tunnel._SERVER.token = ""
    assert _req(info["local_url"] + "/healthz")[0] == 401
    assert _req(info["local_url"] + "/healthz", headers={"Authorization": "Bearer "})[0] == 401


def test_token_not_in_tunnel_json_and_stored_0600(env):
    info = tunnel.start_tunnel(port=0)
    assert info["token"] not in (env / "tunnel.json").read_text()
    assert "token" not in tunnel.get_tunnel_info()
    tf = tunnel.token_file_path(info["port"])
    assert tf.read_text() == info["token"]
    assert (tf.stat().st_mode & 0o777) == 0o600
    assert not str(tf).startswith(str(env))


def test_binds_loopback_by_default(env):
    tunnel.start_tunnel(port=0)
    assert tunnel._SERVER.server_address[0] == "127.0.0.1"


def test_oversized_body_rejected(env):
    info = tunnel.start_tunnel(port=0)
    big = json.dumps({"cmd": "true", "pad": "x" * (tunnel.MAX_BODY_BYTES + 10)}).encode()
    r = urllib.request.Request(
        info["local_url"] + "/exec", data=big, method="POST",
        headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"},
    )
    with pytest.raises((urllib.error.HTTPError, urllib.error.URLError, ConnectionError)) as ei:
        with urllib.request.urlopen(r, timeout=5) as resp:
            pass
    if isinstance(ei.value, urllib.error.HTTPError):
        try:
            assert ei.value.code == 413
        finally:
            ei.value.close()


def test_query_string_token_never_authenticates_post(env):
    info = tunnel.start_tunnel(port=0)
    status, _ = _req(
        f"{info['local_url']}/exec?token={info['token']}",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"cmd": "true"}).encode(),
    )
    assert status == 401


def test_token_not_in_ps_or_argv(env, monkeypatch):
    import nexus_lite.pty as pty
    spawned = []
    real_spawn = pty.spawn

    def wrap_spawn(cmd, **kw):
        spawned.append(cmd)
        return real_spawn(cmd, **kw)

    monkeypatch.setattr(pty, "spawn", wrap_spawn)
    monkeypatch.setattr(tunnel, "ensure_cloudflared", lambda: "/bin/true")
    tunnel.start_tunnel(port=0, token="mysecrettoken123456", tunnel=True, timeout=0.1)
    for c in spawned:
        assert "mysecrettoken123456" not in c


def test_child_process_killed_on_timeout(env):
    info = tunnel.start_tunnel(port=0)
    t0 = time.time()
    st, body = _post(info, "/exec", {"cmd": "sleep 10", "timeout": 0.2}, token=info["token"])
    elapsed = time.time() - t0
    assert elapsed < 3.0, f"exec took {elapsed}s; should have been killed near 0.2s"
    assert st == 504
    data = json.loads(body.decode())
    assert data.get("timed_out") is True
    assert data.get("rc") is None


def test_exec_cwd_is_workspace(env):
    info = tunnel.start_tunnel(port=0)
    st, body = _post(info, "/exec", {"cmd": "pwd"}, token=info["token"])
    assert st == 200
    data = json.loads(body.decode())
    assert Path(data["stdout"].strip()).resolve() == env.resolve()


def test_exec_max_timeout_capped(env, monkeypatch):
    info = tunnel.start_tunnel(port=0)
    captured = {}
    real_run = tunnel._run_with_timeout

    def spy(argv, *, shell, cwd, timeout):
        captured["timeout"] = timeout
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(tunnel, "_run_with_timeout", spy)
    _post(info, "/exec", {"cmd": "true", "timeout": 999999}, token=info["token"])
    assert captured["timeout"] == tunnel.MAX_EXEC_TIMEOUT


def test_stop_tunnel_does_not_kill_stale_pid(env):
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (env / "tunnel.json").write_text(json.dumps({
            "pid": victim.pid, "nonce": "stale", "local_url": "http://127.0.0.1:9",
        }))
        tunnel.stop_tunnel()
        assert victim.poll() is None, "stale pid must not be killed"
    finally:
        victim.kill()
        victim.wait()
    assert not (env / "tunnel.json").exists()


def test_stop_tunnel_kills_verified_tunnel(env, tmp_path):
    sub = subprocess.Popen([
        sys.executable, "-c",
        "import sys, time; import nexus_lite as nl; from nexus_lite.colab import tunnel\n"
        "nl.init(sys.argv[1]); tunnel.start_tunnel(port=0, root=sys.argv[1]); time.sleep(60)\n",
        str(env),
    ])
    try:
        for _ in range(50):
            time.sleep(0.1)
            if (env / "tunnel.json").exists() and tunnel.get_tunnel_info():
                info = tunnel.get_tunnel_info()
                if tunnel._is_recorded_tunnel(json.loads((env / "tunnel.json").read_text())):
                    break
        else:
            pytest.fail("child tunnel did not start")
        tunnel.stop_tunnel()
        assert sub.wait(timeout=5) is not None, "verified child should be killed"
    finally:
        if sub.poll() is None:
            sub.kill()
            sub.wait()


def test_start_tunnel_requires_explicit_token(env):
    with pytest.raises(ValueError):
        tunnel.start_tunnel(port=0, tunnel=True)  # no explicit token


# --- no implicit network installs ---

def test_no_install_without_opt_in(monkeypatch, tmp_path):
    from nexus_lite import core, pty
    import subprocess as sp
    calls = []
    monkeypatch.delenv("NEXUS_LITE_ALLOW_INSTALL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(sp, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(pty, "find_opencode_bin", lambda: None)
    monkeypatch.setattr(tunnel.urllib.request, "urlopen", lambda *a, **k: calls.append("net"))
    assert core.ensure_opencode(install=True) is None
    with pytest.raises(RuntimeError, match="NEXUS_LITE_ALLOW_INSTALL"):
        tunnel.ensure_cloudflared()


def test_cloudflared_sha_verification(monkeypatch, tmp_path):
    class R:
        def __init__(self, data):
            self._d = [data]

        def read(self, n):
            return self._d.pop(0) if self._d else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setenv("NEXUS_LITE_ALLOW_INSTALL", "1")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(tunnel.shutil, "which", lambda n: None)
    monkeypatch.setattr(tunnel.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tunnel.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(tunnel.urllib.request, "urlopen", lambda *a, **k: R(b"evil"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        tunnel.ensure_cloudflared()


def test_ensure_cloudflared_path_preference(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tunnel.shutil, "which", lambda n: "/usr/bin/" + n)
    assert tunnel.ensure_cloudflared() == "/usr/bin/cloudflared"
    assert calls == []


def test_tunnel_server_and_colab_client(env):
    """Test remote tunnel server and ColabTunnelClient end-to-end."""
    info = tunnel.start_tunnel(port=0, tunnel=False)
    try:
        assert info["port"] > 0
        assert info["local_url"].startswith("http://127.0.0.1:")
        assert info["token"]
        assert tunnel.is_tunnel_alive(info, require_tunnel=False)

        client = tunnel.ColabTunnelClient(info["local_url"], token=info["token"])

        # Health
        health = client.health()
        assert health["status"] == "ok"
        assert health["workspace"] == str(env)

        # Exec
        res = client.exec(python="print(10 + 20)")
        assert res["rc"] == 0
        assert "30" in res["stdout"]

        # PTY (requires dtach binary on host)
        if tunnel.shutil.which("dtach"):
            sess = client.pty_spawn("bash --norc", name="tunnel-test-sess")
            assert sess == "tunnel-test-sess"
            client.pty_send(sess, "echo 'PTY_RUN_OK'", enter=True)
            time.sleep(0.3)
            screen = client.pty_read(sess)
            assert "PTY_RUN_OK" in screen
            sessions = client.pty_list()
            assert any(s["name"] == sess for s in sessions)
            client.pty_kill(sess)

        # Bus
        bmsg = client.bus_send(target="test-target", message="bus hello", sender="client")
        assert bmsg["target"] == "test-target"
        msgs = client.bus_read(target="test-target")
        assert len(msgs) == 1
        assert msgs[0]["id"] == bmsg["id"]
        assert msgs[0]["content"] == "bus hello"

        # Delegate
        dres = client.delegate(agent_cmd="python3 -c 'print(\"DELEGATE_OK\")'", prompt="Do task")
        assert dres["rc"] == 0
        assert "DELEGATE_OK" in dres["output"]

        # STM
        snode = client.stm_write(kind="decision", status="done", summary="Tunnel test node")
        assert snode["summary"] == "Tunnel test node"
        read_node = client.stm_read(snode["id"])
        assert read_node["id"] == snode["id"]

        # Wiki
        wpath = client.wiki_write("docs/test_tunnel", "# Remote Tunnel\nTesting content.")
        assert "docs/test_tunnel" in wpath
        wcontent = client.wiki_read("docs/test_tunnel")
        assert "Testing content" in wcontent
        whits = client.wiki_search("Remote Tunnel")
        assert len(whits) >= 1

        # Dashboard HTML
        dhtml = client.dashboard_html()
        assert "<html" in dhtml.lower()

    finally:
        tunnel.stop_tunnel()

    assert not tunnel.is_tunnel_alive()


def test_cli_tunnel(env, capsys):
    """Test CLI commands for tunnel status, exec, and stop."""
    info = tunnel.start_tunnel(port=0, tunnel=False)
    try:
        # status
        rc = tunnel.main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "local_url" in out

        # exec
        rc = tunnel.main(["exec", "echo 'CLI_EXEC_OK'", "--url", info["local_url"], "--token", info["token"]])
        assert rc == 0
        assert "CLI_EXEC_OK" in capsys.readouterr().out
    finally:
        tunnel.main(["stop"])

    assert not tunnel.is_tunnel_alive()


def test_tunnel_refresh_meta_tag(env):
    """Test ?refresh=3 meta tag injection in tunnel dashboard."""
    info = tunnel.start_tunnel(port=0, tunnel=False)
    try:
        req = urllib.request.Request(
            f"{info['local_url']}/dashboard?refresh=3",
            headers={"Authorization": f"Bearer {info['token']}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert "<meta http-equiv='refresh' content='3'>" in html
    finally:
        tunnel.stop_tunnel()
