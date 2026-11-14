"""
nexus_lite.colab.pc — Agent PC: a dedicated, persistent Colab workstation per agent.

World (host commons: `.nexus/` STM / wiki / bus / artifacts) <-> Agent PC (private remote
`work/` + `.nexus/` + cached kernel). Everything here is outside the kernel; the kernel never
imports this module. Every external operation is PHI fail-closed (`phi_gate_error("colab")`).

Secrets: the Bearer token lives only in a 0600 temp token file (`token_file_path("pc-<agent>")`),
never in `<world_ws>/pcs/<agent>.json`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Union

from .. import compute, core
from .. import cas
from ..artifacts import mkdir_private, write_private
from ..memory import ltm as mem_ltm
from ..memory import mtm as mem_mtm
from ..memory import stm as mem_stm
from ..phi import phi_gate_error
from .tunnel import (ColabCliClient, ColabTunnelClient, _write_token_file, colab_new, colab_session_active,
                     colab_stop, read_token_file, token_file_path)

_AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESULT_MARK = "__NEXUS_PC_RESULT__:"
_HEARTBEAT_STALE = 20.0
_TEXT_LIMIT = 65536


def _gate() -> None:
    err = phi_gate_error("colab")
    if err:
        raise PermissionError(err)


def _validate_agent(agent: str) -> str:
    if not isinstance(agent, str) or not agent or agent in (".", "..") or not _AGENT_RE.match(agent):
        raise ValueError(f"invalid agent name: {agent!r}")
    return agent


def _validate_rel(rel: str) -> str:
    norm = posixpath.normpath(str(rel))
    if not rel or posixpath.isabs(norm) or norm == ".." or norm.startswith("../") or "\x00" in rel:
        raise ValueError(f"invalid relative path: {rel!r}")
    return norm


def _pcs_dir() -> Path:
    return mkdir_private(core.get_workspace() / "pcs")


def _meta_path(agent: str) -> Path:
    return _pcs_dir() / f"{_validate_agent(agent)}.json"


def _write_meta(agent: str, meta: dict[str, Any]) -> None:
    meta = {k: v for k, v in meta.items() if k != "token"}
    p = _meta_path(agent)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    write_private(tmp, json.dumps(meta, indent=2))
    os.replace(tmp, p)


# Remote runner: executed on the Agent PC (stdlib only). argv[1] = spec.json path.
RUNNER_SRC = r'''
import contextlib, io, json, os, subprocess, sys, threading, time, traceback
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
job_dir = Path(spec["job_dir"]); work = spec["work_dir"]; ws = spec["workspace"]
job_dir.mkdir(parents=True, exist_ok=True)
(job_dir / "runner.pid").write_text(str(os.getpid()))
stop = threading.Event()
def _beat():
    while not stop.is_set():
        try:
            (job_dir / "heartbeat").write_text(str(time.time()))
        except OSError:
            pass
        stop.wait(1.0)
threading.Thread(target=_beat, daemon=True).start()
os.chdir(work)
sys.path.insert(0, spec["runtime_dir"])

def _rc(v):
    if v is None: return 0
    return v if isinstance(v, int) else 1

out, err = io.StringIO(), io.StringIO()
rc, result = 0, None
t0 = time.time()
try:
    if spec.get("python") is not None:
        import nexus_lite
        sp = Path(work) / ".world_stm.json"
        try: world = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
        except Exception: world = {}
        g = {"__name__": "__pc__", "nexus_lite": nexus_lite, "nl": nexus_lite, "WORKSPACE": Path(ws),
             "WORK_DIR": Path(work), "WORLD_STM": world, "RESULT": None}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(compile(spec["python"], "<agent-pc>", "exec"), g)
            except SystemExit as e:
                rc = _rc(e.code)
            except BaseException:
                traceback.print_exc(); rc = 1
        result = g.get("RESULT")
    elif spec.get("lite_argv") is not None:
        import nexus_lite.cli as cli
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = _rc(cli.main(list(spec["lite_argv"])))
            except SystemExit as e:
                rc = _rc(e.code)
            except BaseException:
                traceback.print_exc(); rc = 1
    elif spec.get("cmd") is not None:
        try:
            p = subprocess.run(spec["cmd"], shell=True, capture_output=True, text=True,
                               timeout=spec.get("timeout"))
            rc = p.returncode; out.write(p.stdout or ""); err.write(p.stderr or "")
        except subprocess.TimeoutExpired:
            rc = 124; err.write("timed out")
except BaseException:
    rc = 1; err.write(traceback.format_exc())
stop.set()
final = {"ok": rc == 0, "status": "completed" if rc == 0 else "failed", "rc": rc,
         "stdout": out.getvalue(), "stderr": err.getvalue(),
         "result": json.loads(json.dumps(result, default=repr)), "elapsed": time.time() - t0,
         "finished_at": time.time()}
tmp = job_dir / "result.json.tmp"
tmp.write_text(json.dumps(final), encoding="utf-8")
os.replace(tmp, job_dir / "result.json")
print("__NEXUS_PC_RESULT__:" + json.dumps(final))
'''

_STAGE_SNIPPET = (
    "import os\n"
    "os.makedirs({job_dir!r}, exist_ok=True)\n"
    "open({runner!r}, 'w', encoding='utf-8').write({runner_src!r})\n"
    "open({spec_path!r}, 'w', encoding='utf-8').write({spec_json!r})\n"
)

_RUN_SNIPPET = (
    "import runpy, sys\n"
    "sys.argv = [{runner!r}, {spec!r}]\n"
    "runpy.run_path({runner!r}, run_name='__main__')\n"
)

_SPAWN_SNIPPET = (
    "import subprocess, sys, json\n"
    "o = open({out!r}, 'wb'); e = open({err!r}, 'wb')\n"
    "p = subprocess.Popen([sys.executable, {runner!r}, {spec!r}], stdin=subprocess.DEVNULL, stdout=o, stderr=e,\n"
    "                     cwd={cwd!r}, start_new_session=True, close_fds=True)\n"
    "print(json.dumps({{'pid': p.pid}}))\n"
)

_STATUS_SNIPPET = r'''
import json, os, time
from pathlib import Path
d = Path({job_dir!r})
res = d / "result.json"
if res.is_file():
    print(json.dumps({{"state": "done", "result": json.loads(res.read_text())}}))
elif not d.is_dir():
    print(json.dumps({{"state": "missing"}}))
else:
    pid = None
    try: pid = int((d / "runner.pid").read_text().strip())
    except Exception: pass
    alive = False
    if pid:
        try:
            os.kill(pid, 0); alive = True
            try:
                if Path(f"/proc/{{pid}}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z": alive = False
            except Exception: pass
        except OSError: pass
    hb = None
    try: hb = time.time() - (d / "heartbeat").stat().st_mtime
    except OSError: pass
    spec_age = None
    try: spec_age = time.time() - (d / "spec.json").stat().st_mtime
    except OSError: pass
    print(json.dumps({{"state": "live", "pid": pid, "alive": alive, "hb_age": hb, "spec_age": spec_age}}))
'''

_PROBE_SNIPPET = r'''
import json, os
rh, sha = {rh!r}, {sha!r}
if rh.startswith("/content/") and os.path.isdir("/content") and os.access("/content", os.W_OK):
    base = "/content/.nexus_runtime/runtime"
else:
    base = rh + "/.runtime"
rd = base + "/" + sha
cached = os.path.isfile(rd + "/nexus_lite/__init__.py")
for p in ({work!r}, {nexus!r}, rh + "/jobs", base):
    os.makedirs(p, exist_ok=True)
print(json.dumps({{"runtime_dir": rd, "base": base, "cached": cached, "nexus_inited": bool(os.listdir({nexus!r}))}}))
'''

_EXTRACT_SNIPPET = r'''
import os, zipfile, shutil
zp, rd = {zp!r}, {rd!r}
tmp = rd + ".tmp-" + str(os.getpid())
shutil.rmtree(tmp, ignore_errors=True)
with zipfile.ZipFile(zp) as zf:
    zf.extractall(tmp)
try:
    os.rename(tmp, rd)
except OSError:
    shutil.rmtree(tmp, ignore_errors=True)
    if not os.path.isfile(rd + "/nexus_lite/__init__.py"):
        raise
os.unlink(zp)
'''


_BUNDLE_CACHE: Optional[tuple[bytes, str]] = None


def _local_bundle() -> tuple[bytes, str]:
    """pack_lite_bundle result, cached: nexus_lite source does not change mid-process."""
    global _BUNDLE_CACHE
    if _BUNDLE_CACHE is None:
        _BUNDLE_CACHE = compute.pack_lite_bundle(include_extras=False)
    return _BUNDLE_CACHE


class AgentPC:
    """One agent's dedicated remote workstation (private work dir, private .nexus, shared cached kernel)."""

    def __init__(self, meta: dict[str, Any], token: str = ""):
        self.meta = dict(meta)
        self.agent: str = meta["agent"]
        self.url: str = meta.get("url") or ""
        self.remote_home: str = meta["remote_home"]
        self.remote_work: str = meta["remote_work"]
        self.remote_nexus: str = meta["remote_nexus"]
        self.bundle_sha: Optional[str] = meta.get("bundle_sha")
        self.runtime_dir: Optional[str] = meta.get("runtime_dir")
        self.client: Union[ColabTunnelClient, ColabCliClient]
        if self.url:
            self.client = ColabTunnelClient(self.url, token)
        elif meta.get("colab_session"):
            self.client = ColabCliClient(meta["colab_session"])
        else:
            raise ValueError("Agent PC needs a tunnel url or a colab_session")

    # ---- helpers ----
    def _save(self, **updates: Any) -> None:
        self.meta.update(updates)
        self.meta["last_seen"] = time.time()
        _write_meta(self.agent, self.meta)

    def _sync_client_env(self) -> None:
        if isinstance(self.client, ColabCliClient):
            self.client.base_env = self._env()

    def _pty_ready(self) -> None:
        if isinstance(self.client, ColabCliClient) and not self.runtime_dir:
            self.ensure_runtime()  # CLI-backed PTY calls import nexus_lite remotely

    def _env(self) -> dict[str, str]:
        env = {
            "NEXUS_LITE_WORKSPACE": self.remote_nexus,
            "NEXUS_LITE_HOME": self.remote_nexus,
            "NEXUS_LITE_DIR": self.remote_nexus,
        }
        if self.runtime_dir:
            env["PYTHONPATH"] = self.runtime_dir
        return env

    def _py(self, code: str, *, timeout: float = 120.0) -> dict[str, Any]:
        """Run a Python snippet on the PC inside its private nexus env; raise on non-zero rc."""
        res = self.client.exec(python=code, cwd=self.remote_work, env=self._env(), timeout=timeout,
                               mkdir_cwd=True)
        if res.get("rc") != 0:
            raise RuntimeError(f"remote python failed rc={res.get('rc')}: {(res.get('stderr') or '')[-800:]}")
        return res

    def _py_json(self, code: str, *, timeout: float = 120.0) -> Any:
        res = self._py(code, timeout=timeout)
        lines = [ln for ln in (res.get("stdout") or "").splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else None

    # ---- M1: runtime ----
    def ensure_runtime(self, force: bool = False) -> dict[str, Any]:
        _gate()
        if not force and self.bundle_sha and self.runtime_dir:
            sha = _local_bundle()[1]
            if self.bundle_sha == sha:  # already verified on the remote: no round-trip
                self._sync_client_env()
                return {"bundle_sha": sha, "runtime_dir": self.runtime_dir, "reused": True,
                        "uploaded_bytes": 0}
        zip_bytes, sha = _local_bundle()
        probe = self.client.exec(python=_PROBE_SNIPPET.format(
            rh=self.remote_home, sha=sha, work=self.remote_work, nexus=self.remote_nexus))
        if probe.get("rc") != 0:
            raise RuntimeError(f"runtime probe failed: {(probe.get('stderr') or '')[-800:]}")
        info = json.loads([ln for ln in probe["stdout"].splitlines() if ln.strip()][-1])
        rd = info["runtime_dir"]
        self.runtime_dir = rd
        self._sync_client_env()
        reused = bool(info["cached"]) and not force
        if not reused:
            zp = f"{info['base']}/.upload-{sha}-{uuid.uuid4().hex[:6]}.zip"
            self.client.fs_write_bytes(zp, zip_bytes)
            if force:
                self.client.exec(python=f"import shutil; shutil.rmtree({rd!r}, ignore_errors=True)")
            ex = self.client.exec(python=_EXTRACT_SNIPPET.format(zp=zp, rd=rd))
            if ex.get("rc") != 0:
                raise RuntimeError(f"bundle extract failed: {(ex.get('stderr') or '')[-800:]}")
        if not reused or not info["nexus_inited"]:
            self._py(f"import nexus_lite; nexus_lite.init({self.remote_nexus!r})")
        self.bundle_sha = sha
        self._save(bundle_sha=sha, runtime_dir=rd)
        return {"bundle_sha": sha, "runtime_dir": rd, "reused": reused,
                "uploaded_bytes": 0 if reused else len(zip_bytes)}

    # ---- M3: execution ----
    def run(self, *, python: Optional[str] = None, cmd: Optional[str] = None,
            lite_argv: Optional[list[str]] = None, mode: str = "fg", job_id: Optional[str] = None,
            collect: Optional[list[str]] = None, timeout: float = 300.0) -> dict[str, Any]:
        _gate()
        if sum(x is not None for x in (python, cmd, lite_argv)) != 1:
            raise ValueError("exactly one of python / cmd / lite_argv is required")
        if mode not in ("fg", "bg"):
            raise ValueError("mode must be 'fg' or 'bg'")
        if job_id is not None and not _AGENT_RE.match(job_id):
            raise ValueError(f"invalid job_id: {job_id!r}")
        rt = self.ensure_runtime()
        jid = job_id or f"job-{uuid.uuid4().hex[:10]}"
        job_dir = f"{self.remote_home}/jobs/{jid}"
        spec = {"job_dir": job_dir, "work_dir": self.remote_work, "workspace": self.remote_nexus,
                "runtime_dir": rt["runtime_dir"], "python": python, "cmd": cmd,
                "lite_argv": lite_argv, "timeout": timeout}
        runner, spec_path = f"{job_dir}/runner.py", f"{job_dir}/spec.json"
        base = {"mode": mode, "agent": self.agent, "job_id": jid, "bundle_sha": self.bundle_sha}
        # One remote exec: write runner.py + spec.json, then run (fg) or spawn detached (bg).
        stage = _STAGE_SNIPPET.format(job_dir=job_dir, runner=runner, runner_src=RUNNER_SRC,
                                      spec_path=spec_path, spec_json=json.dumps(spec))

        if mode == "bg":
            res = self.client.exec(
                python=stage + _SPAWN_SNIPPET.format(out=f"{job_dir}/stdout.log", err=f"{job_dir}/stderr.log",
                                                     runner=runner, spec=spec_path, cwd=self.remote_work),
                env=self._env(), timeout=30.0)
            if res.get("rc") != 0:
                raise RuntimeError(f"bg spawn failed: {(res.get('stderr') or '')[-800:]}")
            pid = json.loads(res["stdout"].strip().splitlines()[-1])["pid"]
            self._save()
            return {"ok": True, "status": "submitted", **base, "pid": pid, "job_dir": job_dir}

        raw = self.client.exec(python=stage + _RUN_SNIPPET.format(runner=runner, spec=spec_path),
                               env=self._env(), timeout=timeout + 10.0)
        if raw.get("timed_out"):
            raise TimeoutError(f"agent PC run timed out after {timeout:g}s")
        final = None
        for ln in (raw.get("stdout") or "").splitlines():
            if ln.startswith(_RESULT_MARK):
                final = json.loads(ln[len(_RESULT_MARK):])
        if final is None:
            final = {"rc": raw.get("rc") if raw.get("rc") is not None else 1,
                     "stdout": raw.get("stdout") or "", "stderr": raw.get("stderr") or "", "result": None}
        rc = final["rc"]
        self._save()
        return {"ok": rc == 0, "status": "completed" if rc == 0 else "failed", **base, "rc": rc,
                "stdout": final["stdout"], "stderr": final["stderr"], "result": final.get("result"),
                "work_dir": self.remote_work, "workspace": self.remote_nexus,
                "artifacts": self._collect(collect or [])}

    def _collect(self, rels: list[str], *, text: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for rel in rels:
            rel = _validate_rel(rel)
            st = self.client.fs_stat(f"{self.remote_work}/{rel}")
            entry: dict[str, Any] = {"exists": st["exists"], "size": st["size"], "sha256": st["sha256"]}
            entry["is_file"] = st["is_file"]
            if text and st["exists"] and st["is_file"] and st["size"] <= _TEXT_LIMIT:
                data = self.client.fs_read_bytes(f"{self.remote_work}/{rel}")
                try:
                    entry["text"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    entry["text"] = None
            out[rel] = entry
        return out

    def job_status(self, job_id: str) -> dict[str, Any]:
        _gate()
        if not _AGENT_RE.match(job_id):
            raise ValueError(f"invalid job_id: {job_id!r}")
        res = self.client.exec(python=_STATUS_SNIPPET.format(job_dir=f"{self.remote_home}/jobs/{job_id}"))
        info = json.loads(res["stdout"].strip().splitlines()[-1])
        base = {"agent": self.agent, "job_id": job_id}
        if info["state"] == "done":
            r = info["result"]
            return {**r, **base, "mode": "bg", "ok": r.get("rc") == 0}
        if info["state"] == "missing":
            return {"ok": False, "status": "failed", **base, "error": "job not found"}
        hb, age = info.get("hb_age"), info.get("spec_age")
        starting = info["pid"] is None and age is not None and age < 10
        stale = hb is not None and hb > _HEARTBEAT_STALE
        if starting or (info["alive"] and not stale):
            return {"ok": True, "status": "running", **base, "pid": info["pid"], "hb_age": hb}
        return {"ok": False, "status": "failed", **base, "pid": info["pid"],
                "error": "runner died without result" if not info["alive"] else "heartbeat stale"}

    def job_wait(self, job_id: str, *, poll_interval: float = 0.25, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            st = self.job_status(job_id)
            if st["status"] in ("completed", "failed"):
                return st
            if time.time() >= deadline:
                raise TimeoutError(f"job {job_id} still running after {timeout:g}s")
            time.sleep(poll_interval)

    # ---- scoped PTYs ----
    def _scoped_pty_name(self, name: str) -> str:
        return name if name.startswith(f"{self.agent}-") else f"{self.agent}-{name}"

    def pty_spawn(self, cmd: str, name: str = "shell") -> str:
        _gate()
        self.ensure_runtime()
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in self._env().items())
        return self.client.pty_spawn(f"export {exports}; {cmd}", self._scoped_pty_name(name),
                                     cwd=self.remote_work)

    def pty_send(self, name: str, text: str, enter: bool = True) -> None:
        _gate()
        self._pty_ready()
        self.client.pty_send(self._scoped_pty_name(name), text, enter=enter)

    def pty_keys(self, name: str, *tokens: str) -> None:
        _gate()
        self._pty_ready()
        self.client.pty_keys(self._scoped_pty_name(name), *tokens)

    def pty_read(self, name: str, **kw: Any) -> str:
        _gate()
        self._pty_ready()
        return self.client.pty_read(self._scoped_pty_name(name), **kw)

    def pty_wait(self, name: str, provider: str = "", timeout: float = 30.0) -> str:
        _gate()
        self._pty_ready()
        return self.client.pty_wait(self._scoped_pty_name(name), provider=provider, timeout=timeout)

    def pty_list(self) -> list[dict]:
        _gate()
        self._pty_ready()
        pre = f"{self.agent}-"
        return [s for s in self.client.pty_list() if str(s.get("name", "")).startswith(pre)]

    def pty_kill(self, name: str) -> None:
        _gate()
        self._pty_ready()
        self.client.pty_kill(self._scoped_pty_name(name))

    # ---- M4: World <-> PC sync ----
    def checkout(self, *, stm: bool = True, wiki_slugs: Optional[list[str]] = None,
                 files: Optional[dict[str, Union[str, Path, bytes]]] = None,
                 bus_unread: bool = True) -> dict[str, Any]:
        _gate()
        self.ensure_runtime()
        summary: dict[str, Any] = {"stm_synced": False, "wiki_synced": [], "files_uploaded": [],
                                   "bus_delivered": 0}
        if stm:
            packed = mem_stm.stm_pack(as_json=True)
            self.client.fs_write_bytes(f"{self.remote_work}/.world_stm.json",
                                       json.dumps(packed, default=repr).encode("utf-8"))
            nodes = []
            for kind in ("project", "decision"):
                for n in mem_stm.stm_list(kind=kind):
                    if kind == "decision" and n.get("status") == "done":
                        continue
                    nodes.append({k: n.get(k) for k in ("id", "kind", "status", "summary", "agent", "task_id", "body")})
            if nodes:
                self._py(
                    "import json\nfrom nexus_lite.memory import stm\n"
                    f"for n in json.loads({json.dumps(json.dumps(nodes))}):\n"
                    "    try:\n"
                    "        stm.stm_write(n['id'], kind=n['kind'] or 'decision', status=n['status'] or 'running',\n"
                    "                      summary=n['summary'] or '', agent=n['agent'], task_id=n['task_id'],\n"
                    "                      body=n['body'] or '')\n"
                    "    except Exception:\n        pass\n")
            summary["stm_synced"] = True
        if wiki_slugs:
            pages = {slug: mem_ltm.wiki_read(slug) for slug in wiki_slugs}
            self._py(
                "import json\nfrom nexus_lite.memory import ltm\n"
                f"for s, c in json.loads({json.dumps(json.dumps(pages))}).items():\n    ltm.wiki_write(s, c)\n")
            summary["wiki_synced"] = list(pages)
        if bus_unread:
            acked = set(self.meta.get("bus_acked_ids") or [])
            msgs = [m for m in mem_mtm.bus_read(target=self.agent, limit=200) if m["id"] not in acked]
            msgs.sort(key=lambda m: m.get("seq") or 0)
            if msgs:
                slim = [{k: m.get(k) for k in ("id", "sender", "target", "kind", "content", "summary", "seq")}
                        for m in msgs]
                self.client.fs_write_bytes(f"{self.remote_work}/.world_inbox.json",
                                           json.dumps(slim).encode("utf-8"))
                self._py(
                    "import json\nfrom nexus_lite.memory import mtm\n"
                    f"for m in json.loads({json.dumps(json.dumps(slim))}):\n"
                    "    try:\n"
                    f"        mtm.bus_send({self.agent!r}, m['content'] or m['summary'] or '',\n"
                    "                     sender=m['sender'], kind=m['kind'] or 'dm')\n"
                    "    except Exception:\n        pass\n")
                acked |= {m["id"] for m in msgs}
                self.meta["bus_acked_ids"] = sorted(acked)
            summary["bus_delivered"] = len(msgs)
        for rel, src in (files or {}).items():
            rel = _validate_rel(rel)
            data = src if isinstance(src, bytes) else Path(src).read_bytes()
            self.client.fs_write_bytes(f"{self.remote_work}/{rel}", data)
            summary["files_uploaded"].append(rel)
        self._save()
        return summary

    def publish(self, *, summary: str, artifacts: Optional[list[str]] = None, stm_kind: str = "decision",
                stm_status: str = "done", wiki_pages: Optional[Union[list[str], dict[str, str]]] = None,
                notify: Optional[str] = "ALL", task_id: Optional[str] = None) -> dict[str, Any]:
        _gate()
        self.ensure_runtime()
        publish_id = f"pub-{uuid.uuid4().hex[:8]}"
        tid = task_id or publish_id
        _validate_agent(tid)
        art_root = core.get_workspace() / "artifacts" / tid
        got, skipped, entries = [], [], {}
        rels = [_validate_rel(r) for r in artifacts or []]
        # remote sha256 comes from _collect (fs_stat); never rehashed here
        for rel, st in self._collect(rels, text=False).items():
            dest = art_root / rel
            sha = st["sha256"]
            if sha and cas.has(sha):  # host CAS already holds it: no download, no re-store
                mkdir_private(dest.parent)
                write_private(dest, cas.get(sha))
                skipped.append(rel)
            else:
                self.client.fs_download(f"{self.remote_work}/{rel}", dest)
                if sha and cas.put(dest, task_id=tid) != sha:
                    raise OSError(f"publish: {rel} changed during download")
            got.append(rel)
            if sha:
                entries[rel] = {"sha256": sha, "size": st["size"]}
        manifest_sha = cas.write_manifest(
            art_root, entries, task_id=tid, ref=f"tasks/{tid}",
            provenance=cas.build_provenance(task_id=tid, parent=cas.ref_get(f"tasks/{tid}"))) if got else None
        node = mem_stm.stm_write(None, kind=stm_kind, status=stm_status, summary=summary,
                                 agent=self.agent, task_id=tid)
        wiki_written: list[str] = []
        if wiki_pages:
            if isinstance(wiki_pages, dict):
                pages = dict(wiki_pages)
            else:
                pages = self._py_json(
                    "import json\nfrom nexus_lite.memory import ltm\n"
                    f"print(json.dumps({{s: ltm.wiki_read(s) for s in {list(wiki_pages)!r}}}))\n")
            for slug, content in pages.items():
                mem_ltm.wiki_write(slug, content)
                wiki_written.append(slug)
        bus_msg = None
        if notify:
            bus_msg = mem_mtm.bus_send(target=notify, sender=self.agent, message=summary)
        self._save()
        return {"ok": True, "agent": self.agent, "task_id": tid, "artifacts": got,
                "skipped": skipped, "manifest_sha256": manifest_sha, "artifact_dir": str(art_root), "stm_node": node, "wiki_written": wiki_written,
                "bus_message": bus_msg}


# ---- registry ----
def bind_pc(agent: str, *, url: Optional[str] = None, token: Optional[str] = None,
            ssh_host: Optional[str] = None, colab_session: Optional[str] = None, gpu: Optional[str] = None,
            tpu: Optional[str] = None, high_mem: bool = False,
            remote_home: Optional[str] = None, bootstrap: bool = True) -> AgentPC:
    _gate()
    _validate_agent(agent)
    existing: dict[str, Any] = {}
    mp = _meta_path(agent)
    if mp.is_file():
        existing = json.loads(mp.read_text(encoding="utf-8"))
    url = (url or existing.get("url") or "").strip().rstrip("/")
    colab_session = (colab_session or "").strip() or None
    if not url:
        # No tunnel: drive a `colab` CLI session, creating it on demand.
        sess = colab_session or existing.get("colab_session")
        if not sess:
            sess = next((c for c in (agent, f"nx-{agent}") if colab_session_active(c)), None)
        if not sess:
            sess = f"nx-{agent}"
            colab_new(session=sess, gpu=gpu, tpu=tpu, high_mem=high_mem)
        elif not colab_session_active(sess):
            colab_new(session=sess, gpu=gpu, tpu=tpu, high_mem=high_mem)
        colab_session = sess
    home = (remote_home or existing.get("remote_home") or f"/content/pc/{agent}").rstrip("/")
    now = time.time()
    meta = {
        "agent": agent, "url": url or None,
        "ssh_host": ssh_host or existing.get("ssh_host"),
        "colab_session": colab_session or existing.get("colab_session"),
        "gpu": gpu or existing.get("gpu"),
        "remote_home": home, "remote_work": f"{home}/work", "remote_nexus": f"{home}/.nexus",
        "bundle_sha": existing.get("bundle_sha"),
        "created_at": existing.get("created_at") or now, "last_seen": now,
        "bus_acked_ids": existing.get("bus_acked_ids") or [],
    }
    tok = token if token is not None else _read_token(agent)
    if token is not None:
        _write_token_file(f"pc-{agent}", token)  # type: ignore[arg-type]
    _write_meta(agent, meta)
    pc = AgentPC(meta, tok)
    if bootstrap:
        pc.ensure_runtime()
    return pc


def _read_token(agent: str) -> str:
    stored = read_token_file(f"pc-{agent}")
    if stored is not None:
        return stored
    return (os.environ.get("NEXUS_COLAB_TOKEN") or "").strip()


def get_pc(agent: str) -> AgentPC:
    _gate()
    mp = _meta_path(agent)
    if not mp.is_file():
        raise KeyError(f"no Agent PC bound for {agent!r}")
    return AgentPC(json.loads(mp.read_text(encoding="utf-8")), _read_token(agent))


def list_pcs() -> list[dict[str, Any]]:
    _gate()
    out = []
    for p in sorted(_pcs_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def unbind_pc(agent: str, *, stop_remote: bool = False) -> bool:
    _gate()
    mp = _meta_path(agent)
    if not mp.is_file():
        return False
    if stop_remote:
        pc = get_pc(agent)
        for s in pc.pty_list():
            try:
                pc.client.pty_kill(s["name"])
            except Exception:
                pass
        if pc.meta.get("colab_session") and not pc.url:
            colab_stop(pc.meta["colab_session"])
    mp.unlink()
    try:
        token_file_path(f"pc-{agent}").unlink()
    except OSError:
        pass
    return True


# ---- CLI (wired from tunnel.main) ----
def cli(args: Any) -> int:
    """Execute a parsed `nexus-lite-colab pc ...` command; JSON on stdout, errors on stderr."""
    import sys

    def emit(obj: Any) -> None:
        print(json.dumps(obj, default=repr))

    try:
        act = args.pc_action
        if act == "bind":
            pc = bind_pc(args.agent, url=args.url, token=args.token, gpu=args.gpu,
                         colab_session=args.colab_session, tpu=args.tpu, high_mem=args.high_mem,
                         remote_home=args.remote_home, bootstrap=not args.no_bootstrap)
            emit({"ok": True, **pc.meta})
            return 0
        if act == "ls":
            emit({"ok": True, "pcs": list_pcs()})
            return 0
        if act == "unbind":
            emit({"ok": True, "unbound": unbind_pc(args.agent, stop_remote=args.stop_remote)})
            return 0
        pc = get_pc(args.agent)
        if act == "status":
            health = pc.client.health()
            emit({"ok": True, **pc.meta, "health": health})
            return 0
        if act == "checkout":
            files = {}
            for spec in args.file or []:
                rel, _, src = spec.partition("=")
                files[rel] = src
            res = pc.checkout(stm=args.stm, wiki_slugs=args.wiki or None, files=files or None,
                              bus_unread=args.bus)
            emit({"ok": True, **res})
            return 0
        if act == "run":
            py = args.python
            if py and os.path.isfile(py):
                py = Path(py).read_text(encoding="utf-8")
            res = pc.run(python=py, cmd=args.cmd, lite_argv=args.lite or None,
                         mode="bg" if args.bg else "fg", job_id=args.job_id,
                         collect=args.collect or None, timeout=args.timeout)
            emit(res)
            return 0 if res.get("ok") else 1
        if act == "poll":
            res = (pc.job_wait(args.job_id, timeout=args.timeout) if args.wait
                   else pc.job_status(args.job_id))
            emit(res)
            return 0 if res.get("ok") else 1
        if act == "pty":
            sub = args.pty_action
            if sub == "new":
                emit({"ok": True, "name": pc.pty_spawn(args.cmd, args.name)})
            elif sub == "send":
                pc.pty_send(args.name, args.text)
                emit({"ok": True})
            elif sub == "keys":
                pc.pty_keys(args.name, *args.tokens)
                emit({"ok": True})
            elif sub == "read":
                emit({"ok": True, "screen": pc.pty_read(args.name)})
            elif sub == "list":
                emit({"ok": True, "sessions": pc.pty_list()})
            elif sub == "kill":
                pc.pty_kill(args.name)
                emit({"ok": True})
            return 0
        if act == "publish":
            res = pc.publish(summary=args.summary, artifacts=args.artifact or None,
                             wiki_pages=args.wiki or None, notify=args.notify, task_id=args.task_id)
            emit(res)
            return 0
        emit({"ok": False, "error": f"unknown pc action {act}"})
        return 1
    except PermissionError as e:
        print(json.dumps({"ok": False, "error": str(e), "code": 78}), file=sys.stderr)
        return 78
    except TimeoutError as e:
        print(json.dumps({"ok": False, "error": str(e), "code": 124}), file=sys.stderr)
        return 124
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), file=sys.stderr)
        return 1
