"""
nexus_lite.colab.tunnel — Cloudflared remote Colab tunnel server and ColabTunnelClient.

Allows any local agent (Claude, Agy, Nexus) or external browser to connect to
a remote Google Colab or headless Linux runtime through an ephemeral
trycloudflare.com tunnel without firewall, port forwarding, or domain setup.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional, Union

from .. import core, pty
from ..memory import ltm as mem_ltm
from ..memory import mtm as mem_mtm
from ..memory import stm as mem_stm

_SERVER: Optional[http.server.ThreadingHTTPServer] = None
_SERVER_THREAD: Optional[threading.Thread] = None

MAX_BODY_BYTES = 1024 * 1024
DEFAULT_EXEC_TIMEOUT = 60.0
MAX_EXEC_TIMEOUT = 600.0
TUNNEL_SERVICE = "nexus-lite-tunnel"


def redact_tunnel_info(info: Optional[dict]) -> Optional[dict]:
    """Copy of tunnel info without the secret token (safe to return, print or persist)."""
    if info is None:
        return None
    return {k: v for k, v in info.items() if k != "token"}


def token_file_path(port_or_key: Union[int, str]) -> Path:
    """Local-only (never Drive) 0600 token file: int port -> tunnel-<port>.token, str key -> <key>.token."""
    name = f"tunnel-{port_or_key}.token" if isinstance(port_or_key, int) else f"{port_or_key}.token"
    return Path(tempfile.gettempdir()) / f"nexus-lite-{os.getuid()}" / name


def _secure_token_dir(create: bool) -> Optional[Path]:
    """The token dir, verified: a real directory (no symlink), owned by this uid, mode 0700.

    The dir lives under the shared temp dir, so a pre-created symlink / foreign-owned dir must
    never be trusted. Own dir with looser bits is tightened; anything else -> PermissionError.
    `create=False` returns None when it does not exist.
    """
    d = token_file_path("x").parent
    if create:
        try:
            os.mkdir(str(d), 0o700)
        except FileExistsError:
            pass
    try:
        st = os.lstat(str(d))
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"token dir {d} is not a plain directory (symlink?)")
    if st.st_uid != os.getuid():
        raise PermissionError(f"token dir {d} is not owned by uid {os.getuid()}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(str(d), 0o700)
        if stat.S_IMODE(os.lstat(str(d)).st_mode) != 0o700:
            raise PermissionError(f"token dir {d} is not mode 0700")
    return d


def _write_token_file(port: Union[int, str], token: str) -> Path:
    path = token_file_path(port)
    _secure_token_dir(create=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(token)
    finally:
        if fd >= 0:
            os.close(fd)
    return path


def read_token_file(port_or_key: Union[int, str]) -> Optional[str]:
    """Token stored by `_write_token_file`, or None when absent. Refuses a symlinked / foreign-owned /
    group-or-world-accessible token file (opened O_NOFOLLOW; checks are on the open fd)."""
    if _secure_token_dir(create=False) is None:
        return None
    try:
        fd = os.open(str(token_file_path(port_or_key)), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as e:  # ELOOP from O_NOFOLLOW on a symlink
        raise PermissionError(f"token file unreadable safely: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise PermissionError("token file is not a private regular file owned by this user")
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = -1
            return f.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)


def resolve_cli_token(token: Optional[str], token_file: Optional[str]) -> Optional[str]:
    """CLI token precedence: --token-file, then deprecated --token (warns: visible in `ps`/history), then
    $NEXUS_LITE_TOKEN. Returns None when none is given."""
    if token_file:
        val = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        if not val:
            raise ValueError(f"token file {token_file} is empty")
        return val
    if token is not None:
        sys.stderr.write("warning: --token is deprecated (the value is visible in process listings and shell "
                         "history); use --token-file PATH or the NEXUS_LITE_TOKEN env var\n")
        return token
    return (os.environ.get("NEXUS_LITE_TOKEN") or "").strip() or None


FS_CHUNK_BYTES = 524288


def _resolve_allowed_path(raw_path: str, server: Any) -> Path:
    """Resolve `raw_path` and confine it to the workspace / workstation roots (else PermissionError)."""
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise PermissionError("Path outside allowed workstation roots")
    ws = core.get_workspace().resolve()
    p = Path(raw_path)
    target = (p if p.is_absolute() else ws / p).resolve()
    roots = [ws, Path("/content/pc").resolve(), Path("/content/.nexus_runtime").resolve()]
    extra = list(getattr(server, "allowed_roots", None) or [])
    extra += [x for x in os.environ.get("NEXUS_LITE_PC_ROOT", "").split(os.pathsep) if x]
    roots += [Path(x).resolve() for x in extra]
    for r in roots:
        if target == r or r in target.parents:
            return target
    raise PermissionError("Path outside allowed workstation roots")


def _run_with_timeout(argv_or_cmd: Any, *, shell: bool, cwd: str, timeout: float,
                      env: Optional[dict] = None) -> dict:
    """Run a command in its own process group; kill the whole group on timeout."""
    proc = subprocess.Popen(
        argv_or_cmd,
        shell=shell,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
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


def _orchestration():
    """Lazy import: demo orchestration lives outside the kernel."""
    from ..demos import orchestration

    return orchestration


class TunnelRequestHandler(http.server.BaseHTTPRequestHandler):
    """
    HTTP JSON API handler for nexus-lite remote tunnel.
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
            # Unauthenticated, secret-free liveness/identity probe used by stop_tunnel/is_tunnel_alive.
            self._send_json(200, {
                "service": TUNNEL_SERVICE,
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
            from ..demos import viz  # lazy: demos are outside the kernel

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
                if payload.get("cwd"):
                    try:
                        rc_dir = _resolve_allowed_path(str(payload["cwd"]), self.server)
                    except PermissionError as e:
                        self._send_json(403, {"error": str(e)})
                        return
                    if payload.get("mkdir_cwd"):
                        rc_dir.mkdir(parents=True, exist_ok=True)
                    if not rc_dir.is_dir():
                        self._send_json(400, {"error": "cwd is not a directory"})
                        return
                    ws_cwd = str(rc_dir)
                extra: dict[str, Any] = {}
                if isinstance(payload.get("env"), dict):
                    run_env = os.environ.copy()
                    run_env.update({k: v for k, v in payload["env"].items()
                                    if isinstance(k, str) and isinstance(v, str)})
                    extra["env"] = run_env

                if "python" in payload and payload["python"] is not None:
                    py_src = str(payload["python"])
                    if len(py_src.encode("utf-8")) > 65_536:
                        with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as tf:
                            tf.write(py_src)
                            tmp_py = tf.name
                        try:
                            res = _run_with_timeout(
                                [sys.executable, tmp_py], shell=False, cwd=ws_cwd, timeout=exec_timeout, **extra
                            )
                        finally:
                            try:
                                os.unlink(tmp_py)
                            except OSError:
                                pass
                    else:
                        res = _run_with_timeout(
                            [sys.executable, "-c", py_src], shell=False, cwd=ws_cwd, timeout=exec_timeout, **extra
                        )
                    self._send_json(504 if res.get("timed_out") else 200, res)
                    return

                if "cmd" in payload and payload["cmd"] is not None:
                    res = _run_with_timeout(payload["cmd"], shell=True, cwd=ws_cwd, timeout=exec_timeout, **extra)
                    self._send_json(504 if res.get("timed_out") else 200, res)
                    return

                self._send_json(400, {"error": "Missing 'cmd' or 'python' parameter in /exec"})
                return

            if path == "/fs":
                self._handle_fs(payload)
                return

            if path == "/pty":
                action = payload.get("action")
                if action == "spawn":
                    spawn_cwd = payload.get("cwd")
                    if spawn_cwd:
                        spawn_cwd = str(_resolve_allowed_path(str(spawn_cwd), self.server))
                    name = pty.spawn(payload["cmd"], name=payload.get("name"), cwd=spawn_cwd)
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
                    msg = mem_mtm.bus_send(
                        target=payload["target"],
                        message=payload["message"],
                        sender=payload.get("sender", "user"),
                        kind=payload.get("kind", "dm"),
                    )
                    self._send_json(200, {"status": "ok", "message": msg})
                    return

                if action == "read":
                    msgs = mem_mtm.bus_read(
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
                    res = _orchestration().run_multi_tier_swarm(
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
                    res = _orchestration().run_agent_interaction(
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
                    res = _orchestration().run_cooperative_problem_solver(
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
                    node = mem_stm.stm_write(
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
                    node = mem_stm.stm_read(payload["node_id"])
                    if node is None:
                        self._send_json(404, {"error": f"STM node not found: {payload['node_id']}"})
                    else:
                        self._send_json(200, {"status": "ok", "node": node})
                    return

                if action == "list":
                    nodes = mem_stm.stm_list(
                        kind=payload.get("kind"),
                        include_invalid=bool(payload.get("include_invalid", False)),
                    )
                    self._send_json(200, {"status": "ok", "nodes": nodes})
                    return

                if action == "search":
                    results = mem_stm.stm_search(
                        payload["query"],
                        limit=int(payload.get("limit", 20)),
                    )
                    self._send_json(200, {"status": "ok", "results": results})
                    return

                if action == "pack":
                    as_json = bool(payload.get("as_json", True))
                    packed = mem_stm.stm_pack(as_json=as_json)
                    self._send_json(200, {"status": "ok", "pack": packed})
                    return

                self._send_json(400, {"error": f"Unknown stm action: {action}"})
                return

            if path == "/wiki":
                action = payload.get("action")
                if action == "write":
                    p = mem_ltm.wiki_write(payload["slug"], payload["content"])
                    self._send_json(200, {"status": "ok", "path": str(p)})
                    return

                if action == "read":
                    content = mem_ltm.wiki_read(payload["slug"])
                    self._send_json(200, {"status": "ok", "content": content})
                    return

                if action == "search":
                    results = mem_ltm.wiki_search(payload["query"])
                    self._send_json(200, {"status": "ok", "results": results})
                    return

                if action == "list":
                    articles = mem_ltm.wiki_list()
                    self._send_json(200, {"status": "ok", "articles": articles})
                    return

                self._send_json(400, {"error": f"Unknown wiki action: {action}"})
                return

            self._send_json(404, {"error": f"Endpoint not found: {path}"})

        except PermissionError as e:
            self._send_json(403, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_fs(self, payload: dict) -> None:
        action = payload.get("action")
        target = _resolve_allowed_path(str(payload.get("path") or ""), self.server)
        if action == "write_chunk":
            raw = zlib.decompress(base64.b64decode(payload["data_zb64"]))
            offset = int(payload.get("offset", 0))
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "wb" if (payload.get("truncate") and offset == 0) else ("r+b" if target.exists() else "wb")
            with open(target, mode) as f:
                f.seek(offset)
                f.write(raw)
            self._send_json(200, {"status": "ok", "path": str(target), "bytes_written": len(raw)})
        elif action == "read_chunk":
            if not target.is_file():
                self._send_json(404, {"error": "File not found"})
                return
            offset = max(0, int(payload.get("offset", 0)))
            length = max(0, min(int(payload.get("length", FS_CHUNK_BYTES)), FS_CHUNK_BYTES))
            total = target.stat().st_size
            with open(target, "rb") as f:
                f.seek(offset)
                chunk = f.read(length)
            zb64 = base64.b64encode(zlib.compress(chunk)).decode("ascii")
            self._send_json(200, {"status": "ok", "size": total, "offset": offset,
                                  "bytes_read": len(chunk), "eof": offset + len(chunk) >= total,
                                  "data_zb64": zb64})
        elif action == "stat":
            if not target.exists():
                self._send_json(200, {"status": "ok", "exists": False, "is_file": False,
                                      "size": 0, "sha256": None})
                return
            is_file = target.is_file()
            digest = None
            if is_file:
                h = hashlib.sha256()
                with open(target, "rb") as f:
                    for blk in iter(lambda: f.read(1 << 20), b""):
                        h.update(blk)
                digest = h.hexdigest()
            self._send_json(200, {"status": "ok", "exists": True, "is_file": is_file,
                                  "size": target.stat().st_size if is_file else 0, "sha256": digest})
        else:
            self._send_json(400, {"error": f"Unknown fs action: {action}"})


class _ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def start_tunnel(
    port: int = 8765,
    token: Optional[str] = None,
    tunnel: bool = False,
    timeout: float = 25.0,
    root: Optional[Union[str, Path]] = None,
    host: str = "127.0.0.1",
) -> dict:
    """
    Start the remote HTTP JSON tunnel server; a cloudflared tunnel only when explicitly requested.

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
    local-only temp dir (see token_file_path), never in tunnel.json.
    """
    global _SERVER, _SERVER_THREAD

    if token is not None and not str(token).strip():
        raise ValueError("tunnel token must not be empty")
    if tunnel and token is None:
        raise ValueError("starting a public tunnel requires an explicit non-empty token")

    core.init(root=root)
    if token is None:
        token = secrets.token_hex(16)
    token = str(token).strip()

    if _SERVER is not None:
        stop_tunnel()

    try:
        server = _ReusableThreadingHTTPServer((host, port), TunnelRequestHandler)
    except OSError:
        server = _ReusableThreadingHTTPServer((host, 0), TunnelRequestHandler)
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
                pty.kill("cloudflared-tunnel")
            except Exception:
                pass
            cf_bin = ensure_cloudflared()
            cmd = f"{cf_bin} tunnel --no-autoupdate --url {local_url}"
            pty.spawn(cmd, name="cloudflared-tunnel")

            pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
            start_t = time.time()
            while time.time() - start_t < timeout:
                time.sleep(0.5)
                screen = pty.read("cloudflared-tunnel")
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

    tunnel_info = {
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
        tunnel_json_path = core.get_workspace() / "tunnel.json"
        tunnel_json_path.write_text(json.dumps(tunnel_info, indent=2), encoding="utf-8")
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
    """GET /identity (unauthenticated, secret-free); None when nothing answers as a lite tunnel."""
    try:
        with urllib.request.urlopen(f"{local_url.rstrip('/')}/identity", timeout=timeout) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict) and payload.get("service") == TUNNEL_SERVICE:
        return payload
    return None


def _is_recorded_tunnel(data: dict) -> bool:
    """True only if the process in tunnel.json answers /identity with the same pid and nonce."""
    local_url = str(data.get("local_url") or "").strip()
    nonce = data.get("nonce")
    if not local_url or not nonce or not data.get("pid"):
        return False
    ident = _probe_identity(local_url)
    return bool(ident and ident.get("nonce") == nonce and ident.get("pid") == data.get("pid"))


def stop_tunnel() -> None:
    """
    Shut down the HTTP tunnel server, kill cloudflared-tunnel dtach session,
    and remove .nexus/tunnel.json.
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
        pty.kill("cloudflared-tunnel")
    except Exception:
        pass

    try:
        ws = core.get_workspace()
        tunnel_json_path = ws / "tunnel.json"
        if tunnel_json_path.exists():
            try:
                data = json.loads(tunnel_json_path.read_text(encoding="utf-8"))
                pid = data.get("pid")
                # Only signal a pid proven (via /identity nonce + pid) to be the recorded lite tunnel;
                # a stale tunnel.json may name a recycled, unrelated pid.
                if pid and pid != os.getpid() and _is_recorded_tunnel(data):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
            except Exception:
                pass
            try:
                tunnel_json_path.unlink()
            except OSError:
                pass
    except Exception:
        pass


def get_tunnel_info() -> Optional[dict]:
    """Return parsed dictionary from .nexus/tunnel.json if tunnel is running."""
    try:
        tunnel_json_path = core.get_workspace() / "tunnel.json"
        if not tunnel_json_path.exists():
            return None
        return redact_tunnel_info(json.loads(tunnel_json_path.read_text(encoding="utf-8")))
    except Exception:
        return None


def is_tunnel_alive(info: Optional[dict] = None, require_tunnel: bool = False) -> bool:
    """
    Verify that the tunnel server recorded in `info` (or `tunnel.json`) is actively listening
    in the current runtime via `/identity`, and optionally that `cloudflared-tunnel` is running.
    Prevents stale `tunnel.json` files persisted on Google Drive from masking dead tunnels.
    """
    data = info if info is not None else get_tunnel_info()
    if not data or not isinstance(data, dict):
        return False
    local_url = str(data.get("local_url") or "").strip().rstrip("/")
    if not local_url:
        return False
    if require_tunnel and not data.get("public_url"):
        return False
    if data.get("public_url"):
        try:
            sp = pty.sock_path("cloudflared-tunnel")
            if not os.path.exists(sp) or not pty._probe_connect(sp):
                return False
        except Exception:
            return False
    ident = _probe_identity(local_url)
    if ident is None:
        return False
    nonce = data.get("nonce")
    return not nonce or ident.get("nonce") == nonce


class ColabTunnelClient:
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
        try:
            resp = urllib.request.urlopen(req, timeout=180.0)
        except urllib.error.HTTPError as e:
            e.close()
            raise
        with resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
            if "application/json" in content_type:
                return json.loads(raw.decode("utf-8"))
            return raw.decode("utf-8")

    def health(self) -> dict:
        """Query server health status and workspace path."""
        return self._request("GET", "/healthz")

    def exec(self, cmd: Optional[str] = None, python: Optional[str] = None, *,
             cwd: Optional[str] = None, env: Optional[dict] = None,
             timeout: Optional[float] = None, mkdir_cwd: bool = False) -> dict:
        """Run a remote shell command or Python snippet (optionally in a scoped cwd / extra env)."""
        payload: dict[str, Any] = {}
        if cmd is not None:
            payload["cmd"] = cmd
        if python is not None:
            payload["python"] = python
        if cwd is not None:
            payload["cwd"] = str(cwd)
            if mkdir_cwd:
                payload["mkdir_cwd"] = True
        if env:
            payload["env"] = {str(k): str(v) for k, v in env.items()}
        if timeout is not None:
            payload["timeout"] = timeout
        try:
            return self._request("POST", "/exec", payload)
        except urllib.error.HTTPError as e:
            if e.code == 504:  # server-side exec timeout still carries a result body
                return {"rc": None, "timed_out": True, "stdout": "", "stderr": "", "error": "exec timed out"}
            raise

    def fs_stat(self, path: str) -> dict:
        return self._request("POST", "/fs", {"action": "stat", "path": str(path)})

    def fs_write_bytes(self, remote_path: str, data: bytes, *, chunk_size: int = FS_CHUNK_BYTES) -> dict:
        """Binary-safe chunked upload (zlib+base64 per chunk); returns size and sha256."""
        chunk_size = max(1, min(int(chunk_size), FS_CHUNK_BYTES))
        offset = 0
        first = True
        while first or offset < len(data):
            chunk = data[offset: offset + chunk_size]
            self._request("POST", "/fs", {
                "action": "write_chunk", "path": str(remote_path), "offset": offset,
                "truncate": first,
                "data_zb64": base64.b64encode(zlib.compress(chunk)).decode("ascii"),
            })
            offset += len(chunk)
            first = False
        return {"path": str(remote_path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def fs_read_bytes(self, remote_path: str, *, chunk_size: int = FS_CHUNK_BYTES) -> bytes:
        chunk_size = max(1, min(int(chunk_size), FS_CHUNK_BYTES))
        out = bytearray()
        while True:
            res = self._request("POST", "/fs", {"action": "read_chunk", "path": str(remote_path),
                                                "offset": len(out), "length": chunk_size})
            out += zlib.decompress(base64.b64decode(res["data_zb64"]))
            if res["eof"] or not res["bytes_read"]:
                return bytes(out)

    def fs_upload(self, local_path: Union[str, Path], remote_path: str, *,
                  chunk_size: int = FS_CHUNK_BYTES) -> dict:
        return self.fs_write_bytes(remote_path, Path(local_path).read_bytes(), chunk_size=chunk_size)

    def fs_download(self, remote_path: str, local_path: Union[str, Path], *,
                    chunk_size: int = FS_CHUNK_BYTES) -> Path:
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.fs_read_bytes(remote_path, chunk_size=chunk_size))
        return dest

    def pty_spawn(self, cmd: str, name: Optional[str] = None, cwd: Optional[str] = None) -> str:
        """Spawn a remote dtach PTY session and return its name."""
        payload: dict[str, Any] = {"action": "spawn", "cmd": cmd}
        if name:
            payload["name"] = name
        if cwd:
            payload["cwd"] = str(cwd)
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
        """Fetch rendered HTML5 visualization dashboard from remote tunnel."""
        res = self._request("GET", "/dashboard")
        return res if isinstance(res, str) else str(res)


# ---- `colab` CLI backend (no cloudflared tunnel): sessions provisioned/driven via the installed `colab` binary ----
COLAB_BIN_ENV = "NEXUS_COLAB_BIN"
_SESSION_RE = re.compile(
    r"^\[(?P<session>[^\]]+)\]\s+(?P<endpoint>\S+)\s*(?P<rest>.*)$")
_EXEC_MARK = "__NEXUS_COLAB_EXEC__:"


def _colab_gate() -> None:
    from ..phi import phi_gate_error  # lazy: only the colab layer needs it

    err = phi_gate_error("colab")
    if err:
        raise PermissionError(err)


def _colab_bin() -> str:
    exe = os.environ.get(COLAB_BIN_ENV) or shutil.which("colab")
    if not exe:
        raise FileNotFoundError("`colab` CLI not found on PATH (pip install colab-cli; or set NEXUS_COLAB_BIN)")
    return exe


def _tighten_colab_cli_perms() -> None:
    """Best-effort: ~/.config/colab-cli dirs 0700, files (colab.log, history/*.jsonl, tokens) 0600."""
    root = Path(os.path.expanduser("~/.config/colab-cli"))
    try:
        if not root.is_dir():
            return
        os.chmod(root, 0o700)
        for dirpath, dirnames, filenames in os.walk(root):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o700)
                except OSError:
                    pass
            for f in filenames:
                try:
                    os.chmod(os.path.join(dirpath, f), 0o600)
                except OSError:
                    pass
    except OSError:
        pass


def _colab_run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    _colab_gate()
    _tighten_colab_cli_perms()
    kw: dict[str, Any] = {}
    if os.name == "posix":
        kw["preexec_fn"] = lambda: os.umask(0o077)
    try:
        return subprocess.run([_colab_bin(), *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, **kw)
    finally:
        _tighten_colab_cli_perms()


def _parse_colab_sessions(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _SESSION_RE.match(line.strip())
        if not m:
            continue
        info: dict[str, Any] = {"session": m["session"], "endpoint": m["endpoint"]}
        for part in m["rest"].split("|"):
            k, _, v = part.partition(":")
            if v and k.strip().lower() in ("hardware", "shape", "variant"):
                info[k.strip().lower()] = v.strip()
        out.append(info)
    return out


def colab_sessions(timeout: float = 60.0) -> list[dict[str, Any]]:
    """`colab sessions` -> [{session, endpoint, hardware, shape, variant}]."""
    p = _colab_run(["sessions"], timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"colab sessions failed rc={p.returncode}: {(p.stderr or p.stdout)[-500:]}")
    return _parse_colab_sessions(p.stdout)


def colab_new(*, session: Optional[str] = None, gpu: Optional[str] = None, tpu: Optional[str] = None,
              high_mem: bool = False, timeout: float = 300.0) -> dict[str, Any]:
    """`colab new [-s S] [--gpu G] [--tpu T] [--high-mem]`; returns the created session's info."""
    _colab_gate()
    before = {s["session"] for s in colab_sessions()}
    args = ["new"]
    if session:
        args += ["-s", session]
    if gpu:
        args += ["--gpu", gpu]
    if tpu:
        args += ["--tpu", tpu]
    if high_mem:
        args.append("--high-mem")
    p = _colab_run(args, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"colab new failed rc={p.returncode}: {(p.stderr or p.stdout)[-500:]}")
    after = colab_sessions()
    created = [s for s in after if s["session"] == session or s["session"] not in before]
    info = created[-1] if created else {"session": session}
    if session and info.get("session") != session:
        info = {**info, "name": session}
    return info


def colab_stop(session: str, timeout: float = 60.0) -> bool:
    """`colab stop -s <session>`; True on success."""
    return _colab_run(["stop", "-s", session], timeout=timeout).returncode == 0


def colab_session_active(session: str) -> bool:
    return any(session in (s["session"], s["endpoint"]) for s in colab_sessions())


def colab_exec_file(session: str, local_py: Union[str, Path], *, timeout: float) -> subprocess.CompletedProcess:
    """`colab exec -s S -f <file> --timeout T` (never `--env`: values leak into colab.log)."""
    return _colab_run(["exec", "-s", session, "-f", str(local_py), "--timeout", f"{timeout:g}"],
                      timeout=timeout + 60.0)


_WRAPPER_SRC = '''\
import json, os, subprocess, sys
_sp = {spec_path!r}
try:
    with open(_sp, encoding="utf-8") as _f:
        spec = json.load(_f)
finally:
    try:
        os.unlink(_sp)
    except OSError:
        pass
try:
    env = dict(os.environ); env.update(spec["env"])
    cwd = spec["cwd"]
    if cwd:
        if spec["mkdir_cwd"]:
            os.makedirs(cwd, exist_ok=True)
        os.chdir(cwd)
    if spec["python"] is not None:
        p = subprocess.run([sys.executable, "-c", spec["python"]], capture_output=True, text=True,
                           env=env, timeout=spec["timeout"])
    else:
        p = subprocess.run(spec["cmd"], shell=True, capture_output=True, text=True, env=env,
                           timeout=spec["timeout"])
    res = {{"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "timed_out": False}}
except subprocess.TimeoutExpired:
    res = {{"rc": None, "stdout": "", "stderr": "", "timed_out": True, "error": "exec timed out"}}
except BaseException as e:
    res = {{"rc": 1, "stdout": "", "stderr": repr(e), "timed_out": False}}
print("{mark}" + json.dumps(res))
'''

REMOTE_TMP_ENV = "NEXUS_COLAB_REMOTE_TMP"


class ColabCliClient:
    """Drop-in for ColabTunnelClient backed by `colab exec/upload/download` on a named Colab session."""

    def __init__(self, session: str):
        if not session or not isinstance(session, str):
            raise ValueError("colab session name is required")
        self.session = session
        self.base_env: dict[str, str] = {}

    @staticmethod
    def _tmp_file(data: bytes, suffix: str) -> str:
        fd, path = tempfile.mkstemp(prefix="nexus-colab-", suffix=suffix)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        except BaseException:
            os.unlink(path)
            raise
        return path

    def health(self) -> dict:
        _colab_gate()
        for s in colab_sessions():
            if self.session in (s["session"], s["endpoint"]):
                return {"status": "ok", "colab_session": self.session, **s}
        raise RuntimeError(f"colab session {self.session!r} is not active")

    def exec(self, cmd: Optional[str] = None, python: Optional[str] = None, *,
             cwd: Optional[str] = None, env: Optional[dict] = None,
             timeout: Optional[float] = None, mkdir_cwd: bool = False) -> dict:
        if (cmd is None) == (python is None):
            raise ValueError("exactly one of cmd / python is required")
        t = float(timeout if timeout is not None else DEFAULT_EXEC_TIMEOUT)
        merged = {**self.base_env, **{str(k): str(v) for k, v in (env or {}).items()}}
        spec = {"env": merged, "cwd": cwd, "mkdir_cwd": bool(mkdir_cwd), "python": python, "cmd": cmd,
                "timeout": t}
        # spec (env/python/cmd) travels via `colab upload` (logs only the op, never contents); the
        # `-f` wrapper holds just the remote path. Never `--env` / inline: colab-cli logs both verbatim.
        local_spec = self._tmp_file(json.dumps(spec).encode("utf-8"), ".json")
        remote_spec = None
        try:
            for d in (os.environ.get(REMOTE_TMP_ENV, "/dev/shm"), "/tmp"):
                cand = f"{d.rstrip('/')}/nx-spec-{uuid.uuid4().hex}.json"
                if _colab_run(["upload", "-s", self.session, local_spec, cand], timeout=60.0).returncode == 0:
                    remote_spec = cand
                    break
        finally:
            os.unlink(local_spec)
        if remote_spec is None:
            raise RuntimeError("colab upload of exec spec failed (refusing to inline env into colab exec -f)")
        path = self._tmp_file(_WRAPPER_SRC.format(spec_path=remote_spec, mark=_EXEC_MARK).encode("utf-8"),
                              ".py")
        try:
            try:
                p = colab_exec_file(self.session, path, timeout=t + 15.0)
            except subprocess.TimeoutExpired:
                return {"rc": None, "timed_out": True, "stdout": "", "stderr": "", "error": "exec timed out"}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        for ln in reversed((p.stdout or "").splitlines()):
            if ln.startswith(_EXEC_MARK):
                try:
                    return json.loads(ln[len(_EXEC_MARK):])
                except ValueError:
                    break
        return {"rc": p.returncode or 1, "stdout": p.stdout or "", "stderr": p.stderr or "",
                "timed_out": False, "error": "no result marker from colab exec"}

    def _py_json(self, code: str, *, timeout: float = 60.0) -> Any:
        res = self.exec(python=code, timeout=timeout)
        if res.get("rc") != 0:
            raise RuntimeError(f"remote python failed rc={res.get('rc')}: {(res.get('stderr') or '')[-500:]}")
        lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else None

    def fs_stat(self, path: str) -> dict:
        return self._py_json(
            "import hashlib, json, os\n"
            f"p = {str(path)!r}\n"
            "ex = os.path.exists(p); isf = os.path.isfile(p)\n"
            "print(json.dumps({'exists': ex, 'is_file': isf, 'size': os.path.getsize(p) if isf else 0,\n"
            "                  'sha256': hashlib.sha256(open(p, 'rb').read()).hexdigest() if isf else None}))\n")

    def fs_write_bytes(self, remote_path: str, data: bytes, *, chunk_size: int = FS_CHUNK_BYTES) -> dict:
        local = self._tmp_file(data, ".bin")
        try:
            p = _colab_run(["upload", "-s", self.session, local, str(remote_path)], timeout=300.0)
            ok = p.returncode == 0
            if not ok:  # parent dir may be missing: mkdir once, retry
                self._py_json(f"import os, json\nos.makedirs(os.path.dirname({str(remote_path)!r}) or '.', "
                              "exist_ok=True)\nprint(json.dumps(1))\n")
                ok = _colab_run(["upload", "-s", self.session, local, str(remote_path)],
                                timeout=300.0).returncode == 0
        finally:
            os.unlink(local)
        if not ok:  # fallback: chunked base64 through exec
            chunk_size = max(1, min(int(chunk_size), 32768))
            for i in range(0, max(len(data), 1), chunk_size):
                b64 = base64.b64encode(data[i:i + chunk_size]).decode("ascii")
                self._py_json(
                    "import base64, json\n"
                    f"open({str(remote_path)!r}, {'wb' if i == 0 else 'ab'!r}).write(base64.b64decode({b64!r}))\n"
                    "print(json.dumps(1))\n")
        return {"path": str(remote_path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def fs_read_bytes(self, remote_path: str, *, chunk_size: int = FS_CHUNK_BYTES) -> bytes:
        fd, local = tempfile.mkstemp(prefix="nexus-colab-dl-")
        os.close(fd)
        try:
            p = _colab_run(["download", "-s", self.session, str(remote_path), local], timeout=300.0)
            if p.returncode == 0:
                return Path(local).read_bytes()
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass
        out = bytearray()  # fallback: base64 read through exec
        step = 32768
        while True:
            res = self._py_json(
                "import base64, json\n"
                f"f = open({str(remote_path)!r}, 'rb'); f.seek({len(out)}); d = f.read({step})\n"
                "print(json.dumps({'b': base64.b64encode(d).decode(), 'n': len(d)}))\n")
            out += base64.b64decode(res["b"])
            if res["n"] < step:
                return bytes(out)

    def fs_upload(self, local_path: Union[str, Path], remote_path: str, **kw: Any) -> dict:
        return self.fs_write_bytes(remote_path, Path(local_path).read_bytes(), **kw)

    def fs_download(self, remote_path: str, local_path: Union[str, Path], **kw: Any) -> Path:
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.fs_read_bytes(remote_path))
        return dest

    # ---- PTY: nexus_lite.pty inside the remote session (mirrors TunnelRequestHandler /pty) ----
    def _pty(self, call: str, *, timeout: float = 60.0) -> Any:
        return self._py_json(
            "import json\nfrom nexus_lite import pty\n"
            f"r = {call}\nprint(json.dumps(r if isinstance(r, (str, list, dict, type(None))) else True))\n",
            timeout=timeout)

    def pty_spawn(self, cmd: str, name: Optional[str] = None, cwd: Optional[str] = None) -> str:
        return self._pty(f"pty.spawn({cmd!r}, name={name!r}, cwd={cwd!r})") or (name or "")

    def pty_send(self, name: str, text: str, enter: bool = True) -> None:
        self._pty(f"pty.send({name!r}, {text!r}, enter={enter!r})")

    def pty_keys(self, name: str, *tokens: str) -> None:
        self._pty(f"pty.keys({name!r}, *{list(tokens)!r})")

    def pty_read(self, name: str, cols: int = 160, rows: int = 45, settle: float = 0.25) -> str:
        return self._pty(f"pty.read({name!r}, cols={cols!r}, rows={rows!r}, settle={settle!r})") or ""

    def pty_wait(self, name: str, provider: str = "", timeout: float = 30.0) -> str:
        return self._pty(f"pty.wait({name!r}, provider={provider!r}, timeout={timeout!r})",
                         timeout=timeout + 30.0) or ""

    def pty_list(self) -> list[dict]:
        return self._pty("pty.list_sessions()") or []

    def pty_kill(self, name: str) -> None:
        self._pty(f"pty.kill({name!r})")


def _colab_cli_main(fn: Any) -> int:
    """Run a `colab` CLI helper; JSON on stdout, errors on stderr (PHI -> 78)."""
    try:
        print(json.dumps(fn(), default=repr))
        return 0
    except PermissionError as e:
        print(json.dumps({"ok": False, "error": str(e), "code": 78}), file=sys.stderr)
        return 78
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint for nexus-lite-colab / python -m nexus_lite.colab.tunnel."""
    parser = argparse.ArgumentParser(
        prog="nexus-lite-colab",
        description="nexus-lite Colab tunnel utility (remote HTTP JSON API & cloudflared tunnel)",
    )
    sub = parser.add_subparsers(dest="subcommand", help="Tunnel operations")

    p_start = sub.add_parser("start", help="Start HTTP tunnel server (loopback; tunnel only with --tunnel --token)")
    p_start.add_argument("--port", "-p", type=int, default=8765, help="Port to listen on (default: 8765)")
    p_start.add_argument("--tunnel", action="store_true", help="Publish a public trycloudflare tunnel (requires --token)")
    p_start.add_argument("--token", "-t", default=None,
                         help="DEPRECATED (visible in ps/history): use --token-file or $NEXUS_LITE_TOKEN")
    p_start.add_argument("--token-file", default=None, help="Read the Bearer token from this file")
    p_start.add_argument("--show-token", action="store_true", help="Print the bearer token to stdout")

    p_stop = sub.add_parser("stop", help="Stop the local tunnel (or, with --session, a `colab` CLI session)")
    p_stop.add_argument("--session", "-s", default=None, help="Colab CLI session to stop via `colab stop`")
    sub.add_parser("status", help="Show tunnel server status")

    p_exec = sub.add_parser("exec", help="Execute remote command on tunnel server")
    p_exec.add_argument("cmd", help="Shell command to execute remotely")
    p_exec.add_argument("--url", "-u", default=None, help="Remote tunnel URL")
    p_exec.add_argument("--token", "-t", default=None,
                        help="DEPRECATED (visible in ps/history): use --token-file or $NEXUS_LITE_TOKEN")
    p_exec.add_argument("--token-file", default=None, help="Read the Bearer token from this file")
    p_exec.add_argument("--session", "-s", default=None, help="Colab CLI session (instead of --url/--token)")

    p_new = sub.add_parser("new", help="Create a Colab CLI session (`colab new`)")
    p_new.add_argument("--session", "-s", default=None)
    p_new.add_argument("--gpu", default=None)
    p_new.add_argument("--tpu", default=None)
    p_new.add_argument("--high-mem", action="store_true")
    sub.add_parser("sessions", aliases=["ls"], help="List active Colab CLI sessions (`colab sessions`)")

    p_pc = sub.add_parser("pc", help="Agent PC: dedicated per-agent Colab workstation (World <-> PC)")
    pcs = p_pc.add_subparsers(dest="pc_action", required=True)
    a = pcs.add_parser("bind"); a.add_argument("agent"); a.add_argument("--url", default=None)
    a.add_argument("--token", default=None, help="DEPRECATED: use --token-file or $NEXUS_LITE_TOKEN")
    a.add_argument("--token-file", default=None); a.add_argument("--gpu", default=None)
    a.add_argument("--session", "-s", dest="colab_session", default=None,
                   help="Colab CLI session name (auto-created via `colab new` if not active)")
    a.add_argument("--tpu", default=None); a.add_argument("--high-mem", action="store_true")
    a.add_argument("--remote-home", default=None); a.add_argument("--no-bootstrap", action="store_true")
    pcs.add_parser("ls")
    a = pcs.add_parser("status"); a.add_argument("agent")
    a = pcs.add_parser("unbind"); a.add_argument("agent"); a.add_argument("--stop-remote", action="store_true")
    a = pcs.add_parser("checkout"); a.add_argument("agent")
    a.add_argument("--stm", action=argparse.BooleanOptionalAction, default=True)
    a.add_argument("--bus", action=argparse.BooleanOptionalAction, default=True)
    a.add_argument("--wiki", action="append"); a.add_argument("--file", action="append",
                                                               help="<remote_rel>=<local_path>")
    a = pcs.add_parser("run"); a.add_argument("agent"); a.add_argument("--python", default=None)
    a.add_argument("--cmd", default=None); a.add_argument("--bg", action="store_true")
    a.add_argument("--job-id", default=None); a.add_argument("--collect", action="append")
    a.add_argument("--timeout", type=float, default=300.0)
    a.add_argument("--lite", nargs=argparse.REMAINDER, default=None, help="nexus_lite argv (must be last)")
    a = pcs.add_parser("poll"); a.add_argument("agent"); a.add_argument("job_id")
    a.add_argument("--wait", action="store_true"); a.add_argument("--timeout", type=float, default=300.0)
    a = pcs.add_parser("pty"); a.add_argument("agent")
    pty_sub = a.add_subparsers(dest="pty_action", required=True)
    b = pty_sub.add_parser("new"); b.add_argument("cmd"); b.add_argument("--name", default="shell")
    b = pty_sub.add_parser("send"); b.add_argument("name"); b.add_argument("text")
    b = pty_sub.add_parser("keys"); b.add_argument("name"); b.add_argument("tokens", nargs="+")
    b = pty_sub.add_parser("read"); b.add_argument("name")
    pty_sub.add_parser("list")
    b = pty_sub.add_parser("kill"); b.add_argument("name")
    a = pcs.add_parser("publish"); a.add_argument("agent"); a.add_argument("--summary", required=True)
    a.add_argument("--artifact", action="append"); a.add_argument("--wiki", action="append")
    a.add_argument("--notify", default="ALL"); a.add_argument("--task-id", default=None)

    args = parser.parse_args(argv)

    if hasattr(args, "token_file"):
        try:
            args.token = resolve_cli_token(args.token, args.token_file)
        except (OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    if args.subcommand == "pc":
        from . import pc as _pc  # lazy: pc imports this module

        return _pc.cli(args)

    if not args.subcommand:
        parser.print_help()
        return 0

    if args.subcommand == "start":
        info = start_tunnel(port=args.port, token=args.token, tunnel=args.tunnel)
        print(f"Tunnel started on port {info['port']}")
        print(f"Local URL: {info['local_url']}")
        if info.get("public_url"):
            print(f"Public URL: {info['public_url']}")
        print(f"Dashboard URL: {info['dashboard_url']}")
        if args.show_token:
            print(f"Token: {info['token']}")
        else:
            print(f"Token file: {token_file_path(info['port'])} (0600)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_tunnel()
            print("Tunnel stopped.")
        return 0

    elif args.subcommand == "stop":
        if args.session:
            return _colab_cli_main(lambda: {"ok": True, "session": args.session,
                                            "stopped": colab_stop(args.session)})
        stop_tunnel()
        print("Tunnel stopped.")
        return 0

    elif args.subcommand == "new":
        return _colab_cli_main(lambda: {"ok": True, **colab_new(
            session=args.session, gpu=args.gpu, tpu=args.tpu, high_mem=args.high_mem)})

    elif args.subcommand in ("sessions", "ls"):
        return _colab_cli_main(lambda: {"ok": True, "sessions": colab_sessions()})

    elif args.subcommand == "status":
        info = get_tunnel_info()
        if not info:
            print("Tunnel is not running.")
        else:
            print(json.dumps(info, indent=2))
        return 0

    elif args.subcommand == "exec":
        if args.session:
            try:
                res = ColabCliClient(args.session).exec(cmd=args.cmd)
            except PermissionError as e:
                print(json.dumps({"ok": False, "error": str(e), "code": 78}), file=sys.stderr)
                return 78
        elif args.url and args.token:
            res = ColabTunnelClient(args.url, args.token).exec(cmd=args.cmd)
        else:
            print("colab exec needs --session or both --url and --token", file=sys.stderr)
            return 2
        if res.get("stdout"):
            sys.stdout.write(res["stdout"])
        if res.get("stderr"):
            sys.stderr.write(res["stderr"])
        return res.get("rc") or 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
