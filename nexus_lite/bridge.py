"""
nexus_lite.bridge — Cloudflared remote Colab bridge server and RemoteColab client.

Allows any local agent (Claude, Agy, Nexus) or external browser to connect to
a remote Google Colab or headless Linux runtime through an ephemeral
trycloudflare.com tunnel without firewall, port forwarding, or domain setup.
"""

from __future__ import annotations

import hmac
import http.server
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional, Union

from . import core, pty, viz

_SERVER: Optional[http.server.ThreadingHTTPServer] = None
_SERVER_THREAD: Optional[threading.Thread] = None

MAX_BODY_BYTES = 1024 * 1024
DEFAULT_EXEC_TIMEOUT = 60.0
MAX_EXEC_TIMEOUT = 600.0
BRIDGE_SERVICE = "nexus-lite-bridge"


def redact_bridge_info(info: Optional[dict]) -> Optional[dict]:
    """Copy of bridge info without the secret token (safe to return, print or persist)."""
    if info is None:
        return None
    return {k: v for k, v in info.items() if k != "token"}


def token_file_path(port: int) -> Path:
    """Local-only (never Drive) 0600 file holding the bridge token for `port`."""
    return Path(tempfile.gettempdir()) / f"nexus-lite-{os.getuid()}" / f"bridge-{port}.token"


def _write_token_file(port: int, token: str) -> Path:
    path = token_file_path(port)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(str(path), 0o600)
    return path


def _run_with_timeout(argv_or_cmd: Any, *, shell: bool, cwd: str, timeout: float) -> dict:
    """Run a command in its own process group; kill the whole group on timeout."""
    proc = subprocess.Popen(
        argv_or_cmd,
        shell=shell,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return {"rc": proc.returncode, "stdout": out, "stderr": err}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=5.0)
        except Exception:
            out, err = "", ""
        return {"rc": None, "timed_out": True, "stdout": out or "", "stderr": err or "",
                "error": f"exec timed out after {timeout:g}s"}


_CLOUDFLARED_VERSION = "2026.10.6"
_CLOUDFLARED_SHA256 = "1d356dac769d0dbf62ebe588c4034824bbff98d5790c2f844cc99e0e2f57462d"


def _cloudflared_cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "nexus_lite" / "bin" / "cloudflared"


def ensure_cloudflared() -> str:
    """
    Locate the cloudflared executable.

    1. System PATH via shutil.which('cloudflared')
    2. Private per-user cache (~/.cache/nexus_lite/bin/cloudflared), never /tmp
    3. Only when NEXUS_LITE_ALLOW_INSTALL=1: download the pinned release for
       Linux amd64 and verify its SHA256 before installing it.

    Otherwise raises RuntimeError with install instructions.
    """
    which_cf = shutil.which("cloudflared")
    if which_cf:
        return which_cf

    cache = _cloudflared_cache_path()
    if cache.is_file() and os.access(str(cache), os.X_OK):
        return str(cache)

    if os.environ.get("NEXUS_LITE_ALLOW_INSTALL") != "1":
        raise RuntimeError(
            "cloudflared not found in PATH. Install it yourself, or set "
            "NEXUS_LITE_ALLOW_INSTALL=1 to download the pinned, checksum-verified release."
        )
    if platform.system().lower() != "linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeError("cloudflared not found in PATH, and pinned download only supports Linux amd64.")

    import hashlib

    url = (
        "https://github.com/cloudflare/cloudflared/releases/download/"
        f"{_CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
    )
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = cache.with_name("cloudflared.download")
    try:
        h = hashlib.sha256()
        req = urllib.request.Request(url, headers={"User-Agent": "nexus-lite"})
        with urllib.request.urlopen(req, timeout=30.0) as resp, open(tmp, "wb") as f:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != _CLOUDFLARED_SHA256:
            raise RuntimeError(
                f"cloudflared checksum mismatch: got {h.hexdigest()}, expected {_CLOUDFLARED_SHA256}"
            )
        tmp.chmod(0o755)
        tmp.replace(cache)
        return str(cache)
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(f"Failed to download cloudflared: {e}") from e


class BridgeRequestHandler(http.server.BaseHTTPRequestHandler):
    """
    HTTP JSON API handler for nexus-lite remote bridge.
    Protected by an `Authorization: Bearer <token>` header only (never the query string).
    """

    server: http.server.ThreadingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress noisy request logs to stderr."""
        pass

    def _send_json(self, status_code: int, data: Any) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authenticate(self) -> bool:
        server_token = getattr(self.server, "token", None)
        if not isinstance(server_token, str) or not server_token.strip():
            # A missing/empty configured token must never disable authentication.
            return False
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        client_token = auth_header[7:].strip()
        return hmac.compare_digest(client_token.encode("utf-8"), server_token.encode("utf-8"))

    def do_GET(self) -> None:
        if urllib.parse.urlparse(self.path).path == "/identity":
            # Unauthenticated, secret-free liveness/identity probe used by stop_bridge/is_bridge_alive.
            self._send_json(200, {
                "service": BRIDGE_SERVICE,
                "pid": os.getpid(),
                "nonce": getattr(self.server, "nonce", ""),
            })
            return

        if not self._authenticate():
            self._send_json(401, {"error": "Unauthorized"})
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/healthz":
            self._send_json(200, {
                "status": "ok",
                "workspace": str(core.get_workspace()),
                "pid": os.getpid(),
                "time": time.time(),
            })
            return

        if path in ("/dashboard", "/"):
            html_content = viz.render_dashboard_html()
            refresh_param = qs.get("refresh", [])
            if refresh_param:
                try:
                    refresh_val = int(refresh_param[0])
                    if refresh_val > 0:
                        meta_tag = f"<meta http-equiv='refresh' content='{refresh_val}'>"
                        if "<head>" in html_content:
                            html_content = html_content.replace("<head>", f"<head>\n{meta_tag}", 1)
                        else:
                            html_content = f"{meta_tag}\n{html_content}"
                except ValueError:
                    pass

            content_bytes = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(content_bytes)))
            self.end_headers()
            self.wfile.write(content_bytes)
            return

        self._send_json(404, {"error": f"Endpoint not found: {path}"})

    def do_POST(self) -> None:
        if not self._authenticate():
            self._send_json(401, {"error": "Unauthorized"})
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_length < 0:
            self._send_json(400, {"error": "Invalid Content-Length"})
            return
        if content_length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(413, {"error": f"Request body exceeds {MAX_BODY_BYTES} bytes"})
            return
        body_bytes = self.rfile.read(content_length)
        if body_bytes:
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"error": f"Invalid JSON payload: {e}"})
                return
        else:
            payload = {}

        try:
            if path == "/exec":
                try:
                    exec_timeout = float(payload.get("timeout", DEFAULT_EXEC_TIMEOUT))
                except (TypeError, ValueError):
                    exec_timeout = DEFAULT_EXEC_TIMEOUT
                exec_timeout = max(0.1, min(exec_timeout, MAX_EXEC_TIMEOUT))
                ws_cwd = str(core.get_workspace())

                if "python" in payload and payload["python"] is not None:
                    res = _run_with_timeout(
                        [sys.executable, "-c", payload["python"]], shell=False, cwd=ws_cwd, timeout=exec_timeout
                    )
                    self._send_json(504 if res.get("timed_out") else 200, res)
                    return

                if "cmd" in payload and payload["cmd"] is not None:
                    res = _run_with_timeout(payload["cmd"], shell=True, cwd=ws_cwd, timeout=exec_timeout)
                    self._send_json(504 if res.get("timed_out") else 200, res)
                    return

                self._send_json(400, {"error": "Missing 'cmd' or 'python' parameter in /exec"})
                return

            if path == "/pty":
                action = payload.get("action")
                if action == "spawn":
                    name = pty.spawn(payload["cmd"], name=payload.get("name"), cwd=payload.get("cwd"))
                    self._send_json(200, {
                        "status": "ok",
                        "name": name,
                        "sock": str(pty.sock_path(name)),
                    })
                    return

                if action == "send":
                    pty.send(payload["name"], payload["text"], enter=payload.get("enter", True))
                    self._send_json(200, {"status": "ok"})
                    return

                if action == "keys":
                    tokens = payload.get("tokens", [])
                    pty.keys(payload["name"], *tokens)
                    self._send_json(200, {"status": "ok"})
                    return

                if action == "read":
                    cols = payload.get("cols", 160)
                    rows = payload.get("rows", 45)
                    settle = payload.get("settle", 0.25)
                    screen = pty.read(payload["name"], cols=cols, rows=rows, settle=settle)
                    self._send_json(200, {"status": "ok", "screen": screen})
                    return

                if action == "wait":
                    provider = payload.get("provider", "")
                    timeout = payload.get("timeout", 30.0)
                    screen = pty.wait(payload["name"], provider=provider, timeout=timeout)
                    self._send_json(200, {"status": "ok", "screen": screen})
                    return

                if action == "list":
                    sessions = pty.list_sessions()
                    self._send_json(200, {"status": "ok", "sessions": sessions})
                    return

                if action == "kill":
                    pty.kill(payload["name"])
                    self._send_json(200, {"status": "ok"})
                    return

                self._send_json(400, {"error": f"Unknown pty action: {action}"})
                return

            if path == "/bus":
                action = payload.get("action")
                if action == "send":
                    msg = core.bus_send(
                        target=payload["target"],
                        message=payload["message"],
                        sender=payload.get("sender", "user"),
                        kind=payload.get("kind", "dm"),
                    )
                    self._send_json(200, {"status": "ok", "message": msg})
                    return

                if action == "read":
                    msgs = core.bus_read(
                        target=payload.get("target"),
                        limit=payload.get("limit", 20),
                    )
                    self._send_json(200, {"status": "ok", "messages": msgs})
                    return

                self._send_json(400, {"error": f"Unknown bus action: {action}"})
                return

            if path == "/delegate":
                action = payload.get("action", "delegate")
                if action == "delegate":
                    cmd = payload.get("agent_cmd") or payload.get("cmd")
                    res = core.delegate(
                        agent_cmd=cmd,
                        prompt=payload["prompt"],
                        caller=payload.get("caller", "main"),
                        task_id=payload.get("task_id"),
                        bg=payload.get("bg", False),
                        timeout=payload.get("timeout", 120.0),
                        fallback_cmd=payload.get("fallback_cmd"),
                    )
                    self._send_json(200, {"status": "ok", "result": res})
                    return

                if action == "swarm":
                    specs = payload["specs"]
                    caller = payload.get("caller", "orchestrator")
                    timeout = payload.get("timeout", 120.0)
                    res = core.run_swarm(specs, caller=caller, timeout=timeout)
                    self._send_json(200, {"status": "ok", "result": res})
                    return

                if action == "pipeline":
                    stages = payload["stages"]
                    initial_prompt = payload.get("initial_prompt", "")
                    caller = payload.get("caller", "orchestrator")
                    timeout = payload.get("timeout", 120.0)
                    res = core.run_pipeline(stages, initial_prompt=initial_prompt, caller=caller, timeout=timeout)
                    self._send_json(200, {"status": "ok", "result": res})
                    return

                if action == "multi_tier_swarm":
                    res = core.run_multi_tier_swarm(
                        payload["goal"],
                        tier0_tasks=payload.get("tier0_tasks"),
                        tier1_tasks=payload.get("tier1_tasks"),
                        tier2_prompt=payload.get("tier2_prompt"),
                        tier0_provider=payload.get("tier0_provider", "opencode"),
                        tier0_model=payload.get("tier0_model", "opencode/big-pickle"),
                        tier1_agents=payload.get("tier1_agents"),
                        tier2_agent=payload.get("tier2_agent", "agy"),
                        phi=bool(payload.get("phi", False)),
                        stm=bool(payload.get("stm", False)),
                        caller=payload.get("caller", "orchestrator"),
                        timeout=float(payload.get("timeout", 120.0)),
                        wiki_slug=payload.get("wiki_slug"),
                    )
                    self._send_json(200, {"status": "ok", "result": res})
                    return

                if action == "interact":
                    res = core.run_agent_interaction(
                        goal=payload.get("goal", "Verify medical imaging coordinate transform and HU windowing"),
                        target_file=payload.get("target_file", "solution.py"),
                        architect_agent=payload.get("architect_agent", "agy"),
                        reviewer_agent=payload.get("reviewer_agent", "opencode/mimo-v2.6-flash-free"),
                        wiki_slug=payload.get("wiki_slug", "interaction/latest_a2a"),
                        timeout=float(payload.get("timeout", 90.0)),
                    )
                    self._send_json(200, {"status": "ok", "result": res})
                    return

                if action == "cooperate":
                    res = core.run_cooperative_problem_solver(
                        goal=payload.get(
                            "goal",
                            "Build and adversarially verify a HIPAA-compliant Radiomics Cohort Integrity Engine",
                        ),
                        module_name=payload.get("module_name"),
                        max_rounds=int(payload.get("max_rounds", 3)),
                        architect_agent=payload.get("architect_agent", "agy"),
                        verifier_agent=payload.get("verifier_agent", "opencode/mimo-v2.6-flash-free"),
                        wiki_slug=payload.get("wiki_slug", "coop/latest_solution"),
                        render_dashboard=bool(payload.get("render_dashboard", True)),
                        timeout=float(payload.get("timeout", 90.0)),
                    )
                    self._send_json(200, {"status": "ok", "result": res})
                    return

                self._send_json(400, {"error": f"Unknown delegate action: {action}"})
                return

            if path == "/stm":
                action = payload.get("action", "list")
                if action == "write":
                    node = core.stm_write(
                        payload.get("node_id"),
                        kind=payload.get("kind", "task"),
                        status=payload.get("status", "running"),
                        summary=payload.get("summary", ""),
                        agent=payload.get("agent", "main"),
                        task_id=payload.get("task_id"),
                        sensitivity=payload.get("sensitivity"),
                        body=payload.get("body", ""),
                    )
                    self._send_json(200, {"status": "ok", "node": node})
                    return

                if action == "read":
                    node = core.stm_read(payload["node_id"])
                    if node is None:
                        self._send_json(404, {"error": f"STM node not found: {payload['node_id']}"})
                    else:
                        self._send_json(200, {"status": "ok", "node": node})
                    return

                if action == "list":
                    nodes = core.stm_list(
                        kind=payload.get("kind"),
                        include_invalid=bool(payload.get("include_invalid", False)),
                    )
                    self._send_json(200, {"status": "ok", "nodes": nodes})
                    return

                if action == "search":
                    results = core.stm_search(
                        payload["query"],
                        limit=int(payload.get("limit", 20)),
                    )
                    self._send_json(200, {"status": "ok", "results": results})
                    return

                if action == "pack":
                    as_json = bool(payload.get("as_json", True))
                    packed = core.stm_pack(as_json=as_json)
                    self._send_json(200, {"status": "ok", "pack": packed})
                    return

                self._send_json(400, {"error": f"Unknown stm action: {action}"})
                return

            if path == "/wiki":
                action = payload.get("action")
                if action == "write":
                    p = core.wiki_write(payload["slug"], payload["content"])
                    self._send_json(200, {"status": "ok", "path": str(p)})
                    return

                if action == "read":
                    content = core.wiki_read(payload["slug"])
                    self._send_json(200, {"status": "ok", "content": content})
                    return

                if action == "search":
                    results = core.wiki_search(payload["query"])
                    self._send_json(200, {"status": "ok", "results": results})
                    return

                if action == "list":
                    articles = core.wiki_list()
                    self._send_json(200, {"status": "ok", "articles": articles})
                    return

                self._send_json(400, {"error": f"Unknown wiki action: {action}"})
                return

            self._send_json(404, {"error": f"Endpoint not found: {path}"})

        except Exception as e:
            self._send_json(500, {"error": str(e)})


class _ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def start_bridge(
    port: int = 8765,
    token: Optional[str] = None,
    tunnel: bool = False,
    timeout: float = 25.0,
    root: Optional[Union[str, Path]] = None,
    host: str = "127.0.0.1",
) -> dict:
    """
    Start the remote HTTP JSON bridge server; a cloudflared tunnel only when explicitly requested.

    Parameters:
    - port: Port to listen on (0 for ephemeral OS-chosen port, default 8765).
    - token: Secret Bearer token. None generates a random one; an empty/blank token is rejected
      (authentication can never be disabled).
    - tunnel: Default False. True publishes a public trycloudflare URL and therefore REQUIRES an
      explicit non-empty `token`.
    - timeout: Max seconds to wait for the trycloudflare public URL.
    - root: Optional workspace root directory.
    - host: Bind address (default loopback 127.0.0.1).

    Returns local_url, public_url, dashboard_url, colab_proxy_url, colab_dashboard_url, token, port.
    URLs never embed the token (send it as `Authorization: Bearer`). The token is stored 0600 in a
    local-only temp dir (see token_file_path), never in bridge.json.
    """
    global _SERVER, _SERVER_THREAD

    if token is not None and not str(token).strip():
        raise ValueError("bridge token must not be empty")
    if tunnel and token is None:
        raise ValueError("starting a public tunnel requires an explicit non-empty token")

    core.init(root=root)
    if token is None:
        token = secrets.token_hex(16)
    token = str(token).strip()

    if _SERVER is not None:
        stop_bridge()

    try:
        server = _ReusableThreadingHTTPServer((host, port), BridgeRequestHandler)
    except OSError:
        server = _ReusableThreadingHTTPServer((host, 0), BridgeRequestHandler)
    nonce = secrets.token_hex(8)
    server.token = token  # type: ignore[attr-defined]
    server.nonce = nonce  # type: ignore[attr-defined]
    server.workspace = core.get_workspace()  # type: ignore[attr-defined]
    actual_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    _SERVER = server
    _SERVER_THREAD = thread

    try:
        _write_token_file(actual_port, token)
    except OSError:
        pass

    local_url = f"http://{host}:{actual_port}"
    public_url: Optional[str] = None
    dashboard_url = f"{local_url}/dashboard"

    if tunnel:
        try:
            try:
                pty.kill("cloudflared-bridge")
            except Exception:
                pass
            cf_bin = ensure_cloudflared()
            cmd = f"{cf_bin} tunnel --no-autoupdate --url {local_url}"
            pty.spawn(cmd, name="cloudflared-bridge")

            pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            start_t = time.time()
            while time.time() - start_t < timeout:
                time.sleep(0.5)
                screen = pty.read("cloudflared-bridge")
                match = pattern.search(screen)
                if match:
                    public_url = match.group(0)
                    dashboard_url = f"{public_url}/dashboard"
                    break
        except Exception:
            # Fall back cleanly without tunnel
            public_url = None

    colab_proxy_url: Optional[str] = None
    colab_dashboard_url: Optional[str] = None
    if "google.colab" in sys.modules:
        try:
            import google.colab.output

            res = google.colab.output.eval_js(f"google.colab.kernel.proxyPort({actual_port})")
            if res:
                colab_proxy_url = str(res)
                base = colab_proxy_url if colab_proxy_url.endswith("/") else f"{colab_proxy_url}/"
                colab_dashboard_url = f"{base}dashboard"
        except Exception:
            colab_proxy_url = None
            colab_dashboard_url = None

    bridge_info = {
        "local_url": local_url,
        "public_url": public_url,
        "dashboard_url": dashboard_url,
        "colab_proxy_url": colab_proxy_url,
        "colab_dashboard_url": colab_dashboard_url,
        "port": actual_port,
        "pid": os.getpid(),
        "nonce": nonce,
        "created_at": time.time(),
    }

    try:
        bridge_json_path = core.get_workspace() / "bridge.json"
        bridge_json_path.write_text(json.dumps(bridge_info, indent=2), encoding="utf-8")
    except Exception:
        pass

    return {
        "local_url": local_url,
        "public_url": public_url,
        "dashboard_url": dashboard_url,
        "colab_proxy_url": colab_proxy_url,
        "colab_dashboard_url": colab_dashboard_url,
        "token": token,
        "port": actual_port,
    }


def _probe_identity(local_url: str, timeout: float = 1.5) -> Optional[dict]:
    """GET /identity (unauthenticated, secret-free); None when nothing answers as a lite bridge."""
    try:
        with urllib.request.urlopen(f"{local_url.rstrip('/')}/identity", timeout=timeout) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict) and payload.get("service") == BRIDGE_SERVICE:
        return payload
    return None


def _is_recorded_bridge(data: dict) -> bool:
    """True only if the process in bridge.json answers /identity with the same pid and nonce."""
    local_url = str(data.get("local_url") or "").strip()
    nonce = data.get("nonce")
    if not local_url or not nonce or not data.get("pid"):
        return False
    ident = _probe_identity(local_url)
    return bool(ident and ident.get("nonce") == nonce and ident.get("pid") == data.get("pid"))


def stop_bridge() -> None:
    """
    Shut down the HTTP bridge server, kill cloudflared-bridge dtach session,
    and remove .nexus/bridge.json.
    """
    global _SERVER, _SERVER_THREAD

    if _SERVER is not None:
        try:
            _SERVER.shutdown()
            _SERVER.server_close()
        except Exception:
            pass
        _SERVER = None
        _SERVER_THREAD = None

    try:
        pty.kill("cloudflared-bridge")
    except Exception:
        pass

    try:
        ws = core.get_workspace()
        bridge_json_path = ws / "bridge.json"
        if bridge_json_path.exists():
            try:
                data = json.loads(bridge_json_path.read_text(encoding="utf-8"))
                pid = data.get("pid")
                # Only signal a pid proven (via /identity nonce + pid) to be the recorded lite bridge;
                # a stale bridge.json may name a recycled, unrelated pid.
                if pid and pid != os.getpid() and _is_recorded_bridge(data):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
            except Exception:
                pass
            try:
                bridge_json_path.unlink()
            except OSError:
                pass
    except Exception:
        pass


def get_bridge_info() -> Optional[dict]:
    """Return parsed dictionary from .nexus/bridge.json if bridge is running."""
    try:
        bridge_json_path = core.get_workspace() / "bridge.json"
        if not bridge_json_path.exists():
            return None
        return redact_bridge_info(json.loads(bridge_json_path.read_text(encoding="utf-8")))
    except Exception:
        return None


def is_bridge_alive(info: Optional[dict] = None, require_tunnel: bool = False) -> bool:
    """
    Verify that the bridge server recorded in `info` (or `bridge.json`) is actively listening
    in the current runtime via `/identity`, and optionally that `cloudflared-bridge` is running.
    Prevents stale `bridge.json` files persisted on Google Drive from masking dead tunnels.
    """
    data = info if info is not None else get_bridge_info()
    if not data or not isinstance(data, dict):
        return False
    local_url = str(data.get("local_url") or "").strip().rstrip("/")
    if not local_url:
        return False
    if require_tunnel and not data.get("public_url"):
        return False
    if data.get("public_url"):
        try:
            sp = pty.sock_path("cloudflared-bridge")
            if not os.path.exists(sp) or not pty._probe_connect(sp):
                return False
        except Exception:
            return False
    ident = _probe_identity(local_url)
    if ident is None:
        return False
    nonce = data.get("nonce")
    return not nonce or ident.get("nonce") == nonce


class RemoteColab:
    """
    Standard library urllib.request client for interacting with a remote
    nexus-lite runtime running in Google Colab or remote Linux environment.
    """

    def __init__(self, url: str, token: str = ""):
        self.url = url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        url = f"{self.url}{path}"
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        data: Optional[bytes] = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=180.0) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "application/json" in content_type:
                return json.loads(raw.decode("utf-8"))
            return raw.decode("utf-8")

    def health(self) -> dict:
        """Query server health status and workspace path."""
        return self._request("GET", "/healthz")

    def exec(self, cmd: Optional[str] = None, python: Optional[str] = None) -> dict:
        """Run a remote shell command or Python snippet."""
        payload: dict[str, Any] = {}
        if cmd is not None:
            payload["cmd"] = cmd
        if python is not None:
            payload["python"] = python
        return self._request("POST", "/exec", payload)

    def pty_spawn(self, cmd: str, name: Optional[str] = None) -> str:
        """Spawn a remote dtach PTY session and return its name."""
        payload: dict[str, Any] = {"action": "spawn", "cmd": cmd}
        if name:
            payload["name"] = name
        res = self._request("POST", "/pty", payload)
        return res.get("name", name or "")

    def pty_send(self, name: str, text: str, enter: bool = True) -> None:
        """Send keystrokes or text to a remote PTY session."""
        self._request("POST", "/pty", {
            "action": "send",
            "name": name,
            "text": text,
            "enter": enter,
        })

    def pty_keys(self, name: str, *tokens: str) -> None:
        """Send special key tokens (Enter, c-c, Tab) to a remote PTY session."""
        self._request("POST", "/pty", {
            "action": "keys",
            "name": name,
            "tokens": list(tokens),
        })

    def pty_read(self, name: str, cols: int = 160, rows: int = 45, settle: float = 0.25) -> str:
        """Read captured terminal screen text from a remote PTY session."""
        res = self._request("POST", "/pty", {
            "action": "read",
            "name": name,
            "cols": cols,
            "rows": rows,
            "settle": settle,
        })
        return res.get("screen", "")

    def pty_wait(self, name: str, provider: str = "", timeout: float = 30.0) -> str:
        """Wait for remote PTY agent to become idle and return screen text."""
        res = self._request("POST", "/pty", {
            "action": "wait",
            "name": name,
            "provider": provider,
            "timeout": timeout,
        })
        return res.get("screen", "")

    def pty_list(self) -> list[dict]:
        """List active remote PTY sessions."""
        res = self._request("POST", "/pty", {"action": "list"})
        return res.get("sessions", [])

    def pty_kill(self, name: str) -> None:
        """Kill a remote PTY session."""
        self._request("POST", "/pty", {"action": "kill", "name": name})

    def bus_send(
        self,
        target: str,
        message: str,
        sender: str = "remote",
        kind: str = "dm",
    ) -> dict:
        """Send message across the remote agent bus and inject pointer."""
        res = self._request("POST", "/bus", {
            "action": "send",
            "target": target,
            "message": message,
            "sender": sender,
            "kind": kind,
        })
        return res.get("message", res)

    def bus_read(self, target: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Read messages from the remote message bus."""
        payload: dict[str, Any] = {"action": "read", "limit": limit}
        if target:
            payload["target"] = target
        res = self._request("POST", "/bus", payload)
        return res.get("messages", [])

    def delegate(
        self,
        agent_cmd: str,
        prompt: str,
        caller: str = "main",
        timeout: float = 120.0,
        fallback_cmd: Optional[str] = None,
    ) -> dict:
        """Delegate a task to a remote worker agent (with optional automatic quota/auth fallback_cmd)."""
        payload: dict[str, Any] = {
            "action": "delegate",
            "agent_cmd": agent_cmd,
            "prompt": prompt,
            "caller": caller,
            "timeout": timeout,
        }
        if fallback_cmd:
            payload["fallback_cmd"] = fallback_cmd
        res = self._request("POST", "/delegate", payload)
        return res.get("result", res)

    def run_swarm(
        self,
        specs: list[dict],
        caller: str = "orchestrator",
        timeout: float = 120.0,
    ) -> list[dict]:
        """Execute parallel swarm tasks across multiple remote workers."""
        res = self._request("POST", "/delegate", {
            "action": "swarm",
            "specs": specs,
            "caller": caller,
            "timeout": timeout,
        })
        return res.get("result", res)

    def run_pipeline(
        self,
        stages: list[dict],
        initial_prompt: str,
        caller: str = "orchestrator",
        timeout: float = 120.0,
    ) -> dict:
        """Execute sequential pipeline stages with inter-stage handoffs."""
        res = self._request("POST", "/delegate", {
            "action": "pipeline",
            "stages": stages,
            "initial_prompt": initial_prompt,
            "caller": caller,
            "timeout": timeout,
        })
        return res.get("result", res)

    def run_multi_tier_swarm(
        self,
        goal: str,
        *,
        tier0_tasks: Optional[list[dict[str, Any]]] = None,
        tier1_tasks: Optional[list[dict[str, Any]]] = None,
        tier2_prompt: Optional[str] = None,
        tier0_provider: str = "opencode",
        tier0_model: str = "opencode/big-pickle",
        tier1_agents: Optional[list[str]] = None,
        tier2_agent: str = "agy",
        phi: bool = False,
        stm: bool = False,
        caller: str = "orchestrator",
        timeout: float = 120.0,
        wiki_slug: Optional[str] = None,
    ) -> dict:
        """Execute 3-tier swarm orchestration (Tier 0 Scout -> Tier 1 OpenCode Swarm -> Tier 2 AGY Verifier) on remote host."""
        res = self._request("POST", "/delegate", {
            "action": "multi_tier_swarm",
            "goal": goal,
            "tier0_tasks": tier0_tasks,
            "tier1_tasks": tier1_tasks,
            "tier2_prompt": tier2_prompt,
            "tier0_provider": tier0_provider,
            "tier0_model": tier0_model,
            "tier1_agents": tier1_agents,
            "tier2_agent": tier2_agent,
            "phi": phi,
            "stm": stm,
            "caller": caller,
            "timeout": timeout,
            "wiki_slug": wiki_slug,
        })
        return res.get("result", res)

    def run_agent_interaction(
        self,
        goal: str = "Verify medical imaging coordinate transform and HU windowing",
        *,
        target_file: str = "solution.py",
        architect_agent: str = "agy",
        reviewer_agent: str = "opencode/mimo-v2.6-flash-free",
        wiki_slug: str = "interaction/latest_a2a",
        timeout: float = 90.0,
    ) -> dict:
        """Execute 5-turn peer-to-peer Agent-to-Agent (A2A) interaction loop on remote host."""
        res = self._request("POST", "/delegate", {
            "action": "interact",
            "goal": goal,
            "target_file": target_file,
            "architect_agent": architect_agent,
            "reviewer_agent": reviewer_agent,
            "wiki_slug": wiki_slug,
            "timeout": timeout,
        })
        return res.get("result", res)

    def run_cooperative_problem_solver(
        self,
        goal: str = "Build and adversarially verify a HIPAA-compliant Radiomics Cohort Integrity Engine",
        *,
        module_name: Optional[str] = None,
        max_rounds: int = 3,
        architect_agent: str = "agy",
        verifier_agent: str = "opencode/mimo-v2.6-flash-free",
        wiki_slug: str = "coop/latest_solution",
        render_dashboard: bool = True,
        timeout: float = 90.0,
    ) -> dict:
        """Execute 5-phase autonomous multi-agent cooperative problem-solving swarm on remote host."""
        res = self._request("POST", "/delegate", {
            "action": "cooperate",
            "goal": goal,
            "module_name": module_name,
            "max_rounds": max_rounds,
            "architect_agent": architect_agent,
            "verifier_agent": verifier_agent,
            "wiki_slug": wiki_slug,
            "render_dashboard": render_dashboard,
            "timeout": timeout,
        })
        return res.get("result", res)

    def stm_write(
        self,
        *,
        kind: str,
        status: str,
        summary: str,
        node_id: Optional[str] = None,
        agent: str = "remote",
        task_id: str = "",
        refs: Optional[list[str]] = None,
        body: str = "",
    ) -> dict:
        """Write an stm/1 node to the remote workspace."""
        res = self._request("POST", "/stm", {
            "action": "write",
            "kind": kind,
            "status": status,
            "summary": summary,
            "node_id": node_id,
            "agent": agent,
            "task_id": task_id,
            "refs": refs,
            "body": body,
        })
        return res.get("node", res)

    def stm_read(self, node_id: str) -> Optional[dict]:
        """Read an stm/1 node from the remote workspace."""
        res = self._request("POST", "/stm", {"action": "read", "node_id": node_id})
        return res.get("node")

    def stm_list(
        self,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """List stm/1 nodes in the remote workspace."""
        res = self._request("POST", "/stm", {
            "action": "list",
            "kind": kind,
            "status": status,
            "limit": limit,
        })
        return res.get("nodes", [])

    def stm_search(self, query: str, limit: int = 20) -> list[dict]:
        """Search stm/1 nodes in the remote workspace."""
        res = self._request("POST", "/stm", {
            "action": "search",
            "query": query,
            "limit": limit,
        })
        return res.get("results", [])

    def stm_pack(self, as_json: bool = True) -> Any:
        """Pack stm/1 short-term memory envelope from the remote workspace."""
        res = self._request("POST", "/stm", {
            "action": "pack",
            "as_json": as_json,
        })
        return res.get("pack", res)

    def wiki_write(self, slug: str, content: str) -> str:
        """Write a markdown document to the remote wiki."""
        res = self._request("POST", "/wiki", {
            "action": "write",
            "slug": slug,
            "content": content,
        })
        return res.get("path", "")

    def wiki_read(self, slug: str) -> str:
        """Read a markdown document from the remote wiki."""
        res = self._request("POST", "/wiki", {
            "action": "read",
            "slug": slug,
        })
        return res.get("content", "")

    def wiki_search(self, query: str) -> list[dict]:
        """Perform full-text search across the remote wiki."""
        res = self._request("POST", "/wiki", {
            "action": "search",
            "query": query,
        })
        return res.get("results", [])

    def wiki_list(self) -> list[dict]:
        """List all articles in the remote wiki."""
        res = self._request("POST", "/wiki", {"action": "list"})
        return res.get("articles", [])

    def dashboard_html(self) -> str:
        """Fetch rendered HTML5 visualization dashboard from remote bridge."""
        res = self._request("GET", "/dashboard")
        return res if isinstance(res, str) else str(res)
