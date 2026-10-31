"""Secure bridge defaults. Hermetic: loopback only, no network, mocked tunnel."""

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
from nexus_lite import bridge, core


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    socks = tmp_path / "s"
    socks.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(socks))
    monkeypatch.delenv("NEXUS_LITE_BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    nl.init(ws)
    yield ws
    bridge.stop_bridge()


def _req(url, *, method="GET", headers=None, body=None):
    r = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(info, path, payload, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return _req(info["local_url"] + path, method="POST", headers=h, body=json.dumps(payload).encode())


def test_header_only_token(env):
    info = bridge.start_bridge(port=0)
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
        bridge.start_bridge(port=0, token=tok)
    assert bridge._SERVER is None


def test_empty_server_token_never_authenticates(env):
    info = bridge.start_bridge(port=0)
    bridge._SERVER.token = ""
    assert _req(info["local_url"] + "/healthz")[0] == 401
    assert _req(info["local_url"] + "/healthz", headers={"Authorization": "Bearer "})[0] == 401


def test_token_not_in_bridge_json_and_stored_0600(env):
    info = bridge.start_bridge(port=0)
    assert info["token"] not in (env / "bridge.json").read_text()
    assert "token" not in bridge.get_bridge_info()
    tf = bridge.token_file_path(info["port"])
    assert tf.read_text() == info["token"]
    assert (tf.stat().st_mode & 0o777) == 0o600
    assert not str(tf).startswith(str(env))


def test_binds_loopback_by_default(env):
    bridge.start_bridge(port=0)
    assert bridge._SERVER.server_address[0] == "127.0.0.1"


def test_oversized_body_rejected(env):
    info = bridge.start_bridge(port=0)
    big = json.dumps({"cmd": "true", "pad": "x" * (bridge.MAX_BODY_BYTES + 10)}).encode()
    r = urllib.request.Request(
        info["local_url"] + "/exec", data=big, method="POST",
        headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"},
    )
    with pytest.raises((urllib.error.HTTPError, urllib.error.URLError, ConnectionError)) as ei:
        urllib.request.urlopen(r, timeout=10)
    if isinstance(ei.value, urllib.error.HTTPError):
        assert ei.value.code == 413


def test_exec_ok_and_timeout_kills_group(env, tmp_path):
    info = bridge.start_bridge(port=0)
    code, body = _post(info, "/exec", {"cmd": "echo hi"}, info["token"])
    assert code == 200 and json.loads(body)["stdout"].strip() == "hi"
    marker = tmp_path / "grandchild"
    cmd = f"(sleep 3; touch {marker}) & sleep 30"
    t0 = time.time()
    code, body = _post(info, "/exec", {"cmd": cmd, "timeout": 1}, info["token"])
    assert code == 504 and json.loads(body)["timed_out"] is True
    assert time.time() - t0 < 10
    time.sleep(3.5)
    assert not marker.exists()  # grandchild died with the process group


def test_exec_python_timeout(env):
    info = bridge.start_bridge(port=0)
    code, body = _post(info, "/exec", {"python": "import time; time.sleep(30)", "timeout": 1}, info["token"])
    assert code == 504


@pytest.mark.parametrize("slug", ["../escape", "a/../../escape", "/etc/passwd", "x/../../../y", ""])
def test_wiki_slug_traversal_rejected(env, slug):
    with pytest.raises(ValueError):
        core.wiki_write(slug, "x")
    with pytest.raises(ValueError):
        core.wiki_read(slug)
    assert not (env / "escape.md").exists()


def test_wiki_slug_traversal_rejected_via_bridge(env):
    info = bridge.start_bridge(port=0)
    code, _ = _post(info, "/wiki", {"action": "write", "slug": "../../pwned", "content": "x"}, info["token"])
    assert code == 500
    assert not (env.parent / "pwned.md").exists()
    code, _ = _post(info, "/wiki", {"action": "write", "slug": "ok/doc", "content": "fine"}, info["token"])
    assert code == 200


def test_stop_bridge_does_not_kill_stale_pid(env):
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (env / "bridge.json").write_text(json.dumps({
            "pid": victim.pid, "nonce": "stale", "local_url": "http://127.0.0.1:1", "port": 1,
        }))
        bridge.stop_bridge()
        time.sleep(0.3)
        assert victim.poll() is None  # still alive
        assert not (env / "bridge.json").exists()
    finally:
        victim.kill()
        victim.wait()


def test_stop_bridge_kills_verified_bridge(env, tmp_path):
    script = (
        "import sys, time, nexus_lite as nl\n"
        "from nexus_lite import bridge\n"
        "nl.init(sys.argv[1]); bridge.start_bridge(port=0, root=sys.argv[1]); time.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", script, str(env)], env=dict(os.environ))
    try:
        for _ in range(100):
            if (env / "bridge.json").exists() and bridge.get_bridge_info():
                info = bridge.get_bridge_info()
                if bridge._is_recorded_bridge(json.loads((env / "bridge.json").read_text())):
                    break
            time.sleep(0.1)
        else:
            pytest.fail("child bridge did not start")
        bridge.stop_bridge()
        assert p.wait(timeout=10) is not None
    finally:
        if p.poll() is None:
            p.kill()


def test_tunnel_off_by_default_and_needs_token(env, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "ensure_cloudflared", lambda: calls.append("cf") or "/bin/true")
    monkeypatch.setattr(bridge.pty, "spawn", lambda *a, **k: calls.append("spawn"))
    info = bridge.start_bridge(port=0)
    assert calls == [] and info["public_url"] is None
    bridge.stop_bridge()
    with pytest.raises(ValueError):
        bridge.start_bridge(port=0, tunnel=True)  # no explicit token
    assert calls == []


def test_ensure_colab_defaults_no_tunnel_no_token(env, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(bridge, "ensure_cloudflared", lambda: calls.append("cf") or "/bin/true")
    monkeypatch.setattr(bridge.pty, "spawn", lambda *a, **k: calls.append("spawn"))
    monkeypatch.setattr(core.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setitem(sys.modules, "google.colab", type(sys)("google.colab"))  # looks like a notebook
    res = core.ensure_colab(mount_drive=False, install_opencode=False, port=0)
    assert calls == []
    assert res.bridge is None
    assert "token" not in json.dumps(res, default=str)
    assert capsys.readouterr().out == ""
    assert not (env / "bridge.json").exists()


def test_ensure_colab_tunnel_requires_explicit_token(env, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda n: "/usr/bin/" + n)
    with pytest.raises(ValueError):
        core.ensure_colab(mount_drive=False, install_opencode=False, start_tunnel=True)


def test_ensure_colab_bridge_result_redacted(env, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda n: "/usr/bin/" + n)
    res = core.ensure_colab(mount_drive=False, install_opencode=False, start_bridge_server=True, port=0)
    assert res.bridge and "token" not in res.bridge
    assert "token" not in (env / "bridge.json").read_text()
    shown = core.ensure_colab(mount_drive=False, install_opencode=False, start_bridge_server=True,
                              show_token=True, port=0)
    assert shown.bridge  # alive existing bridge is reused redacted; explicit start path covered above


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
    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *a, **k: calls.append("net"))
    assert core.ensure_opencode(install=True) is None
    with pytest.raises(RuntimeError, match="NEXUS_LITE_ALLOW_INSTALL"):
        bridge.ensure_cloudflared()
    assert calls == []


def test_cloudflared_checksum_mismatch_aborts(monkeypatch, tmp_path):
    import io
    monkeypatch.setenv("NEXUS_LITE_ALLOW_INSTALL", "1")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(bridge.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bridge.platform, "machine", lambda: "x86_64")

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *a, **k: R(b"evil"))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        bridge.ensure_cloudflared()
    assert not (tmp_path / "nexus_lite" / "bin" / "cloudflared").exists()


def test_no_curl_pipe_bash_in_package():
    root = Path(bridge.__file__).parent
    for f in root.rglob("*.py"):
        assert not re.search(r"curl[^\n]*\|\s*(ba)?sh", f.read_text()), f
