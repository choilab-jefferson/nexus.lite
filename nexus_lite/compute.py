"""
nexus_lite.compute — Zero-preinstall remote compute dispatch for Turing HPC (internal) and Google Colab (external).

Core philosophy ("Ship `lite` and compute"):
- Packs the local `nexus_lite` stdlib-only source tree into a compact in-memory ZIP (~140 KB).
- Generates a self-extracting Python runner that unpacks `nexus_lite` on the remote host
  into `~/.cache/nexus_lite/runtime/<bundle_sha>` (or `/tmp/nexus_lite_runtime/<bundle_sha>`),
  initializes an isolated `.nexus` workspace, and runs Python code, shell commands, or
  `nexus-lite` CLI workflows—even on bare machines where `nexus` or `nexus-lite` was never installed.
- Supports:
  1. `turing` / `hpc` (Internal SLURM HPC): SSH to `NEXUS_TURING_HOST` / `NEXUS_HPC_HOST` (default SSH alias
     `turing`, with optional jump-host fallback via `NEXUS_TURING_JUMP_HOST` / `NEXUS_HPC_JUMP_HOST`) and
     execution via `srun` (compute/GPU node), `sbatch` (batch job), or `ssh` (login node).
  2. `colab` (External Compute): Execution via HTTP tunnel (`POST /exec` with self-extracting
     `nexus_lite` bundle), SSH tunnel, or standalone self-bootstrapping script generation for
     bare Colab notebooks / Google Drive.
  3. `local` (Local Sandbox): Unpacks the bundle into a clean temporary directory and executes
     without touching the caller's installed packages.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any, Optional

RESULT_MARKER = "__NEXUS_LITE_COMPUTE_JSON__:"
DEFAULT_TURING_HOST = "turing"
DEFAULT_TURING_JUMP_HOST = ""

# Non-kernel content, shipped only with include_extras: the whole `demos/` package
# plus the deprecated top-level shim modules that forward into it. The remote runner
# (and nexus_lite.colab, which only lazy-imports demos.viz) never needs them.
_OPTIONAL_EXTRA_DIRS = {"demos"}
_OPTIONAL_EXTRA_FILES = {"viz.py", "loom_multi_agent.py", "loom_multi_task.py", "loom_radiomics_tasks.py"}


def pack_lite_bundle(include_extras: bool = True) -> tuple[bytes, str]:
    """Pack the local `nexus_lite` package into a deflated ZIP archive in memory.

    Returns `(zip_bytes, sha256_12)`.
    """
    pkg_root = Path(__file__).resolve().parent
    buf = io.BytesIO()
    hasher = hashlib.sha256()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for py_file in sorted(pkg_root.rglob("*.py")):
            rel = py_file.relative_to(pkg_root)
            if "__pycache__" in rel.parts:
                continue
            if not include_extras and (
                rel.parts[0] in _OPTIONAL_EXTRA_DIRS
                or (len(rel.parts) == 1 and rel.name in _OPTIONAL_EXTRA_FILES)
            ):
                continue
            arcname = f"nexus_lite/{rel.as_posix()}"
            data = py_file.read_bytes()
            hasher.update(arcname.encode("utf-8") + b"\0" + data + b"\0")
            info = zipfile.ZipInfo(arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
        # `nexus_lite/__init__.py` reads the version from an adjacent pyproject.toml
        # when package metadata is absent (the case for an unpacked bundle).
        from . import __version__
        stub = f'version = "{__version__}"\n'.encode("utf-8")
        hasher.update(b"pyproject.toml\0" + stub + b"\0")
        zf.writestr("pyproject.toml", stub)
    raw = buf.getvalue()
    return raw, hasher.hexdigest()[:12]


def pack_self_contained_script(
    *,
    python: Optional[str] = None,
    cmd: Optional[str] = None,
    lite_argv: Optional[list[str]] = None,
    files: Optional[dict[str, str]] = None,
    collect_files: Optional[list[str]] = None,
    job_id: Optional[str] = None,
    target: str = "remote",
    mode: str = "direct",
    include_extras: bool = True,
    start_tunnel: bool = False,
    tunnel_port: int = 8765,
) -> tuple[str, str, str]:
    """Build a standalone, zero-dependency Python script that embeds `nexus_lite` and a workload.

    Returns `(script_source, bundle_sha, job_id)`.
    """
    zip_bytes, bundle_sha = pack_lite_bundle(include_extras=include_extras)
    b64_bundle = base64.b64encode(zip_bytes).decode("ascii")
    resolved_job_id = job_id or f"job-{uuid.uuid4().hex[:8]}"

    spec = {
        "bundle_sha": bundle_sha,
        "job_id": resolved_job_id,
        "target": target,
        "mode": mode,
        "python": python,
        "cmd": cmd,
        "lite_argv": list(lite_argv) if lite_argv is not None else None,
        "files": dict(files or {}),
        "collect_files": list(collect_files or []),
        "start_tunnel": bool(start_tunnel),
        "tunnel_port": int(tunnel_port),
    }
    spec_json = json.dumps(spec, ensure_ascii=False)
    spec_zlib_b64 = base64.b64encode(zlib.compress(spec_json.encode("utf-8"), level=9)).decode("ascii")

    script = f'''#!/usr/bin/env python3
# Auto-generated self-contained nexus_lite compute runner (bundle={bundle_sha}, job={resolved_job_id})
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
import zlib
from pathlib import Path

BUNDLE_B64 = {b64_bundle!r}
SPEC = json.loads(zlib.decompress(base64.b64decode({spec_zlib_b64!r})).decode("utf-8"))
RESULT_MARKER = {RESULT_MARKER!r}


def _resolve_cache_root() -> Path:
    env_root = os.environ.get("NEXUS_LITE_RUNTIME_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    if Path("/content").is_dir() and os.access("/content", os.W_OK):
        return Path("/content/.nexus_runtime").resolve()
    home = Path.home()
    if home.is_dir() and os.access(str(home), os.W_OK):
        return (home / ".cache" / "nexus_lite").resolve()
    return (Path(tempfile.gettempdir()) / f"nexus_lite_{{os.getuid()}}").resolve()


def _ensure_bundle_extracted(cache_root: Path, bundle_sha: str) -> Path:
    runtime_dir = cache_root / "runtime" / bundle_sha
    marker = runtime_dir / "nexus_lite" / "__init__.py"
    if marker.is_file():
        return runtime_dir
    tmp_dir = cache_root / "runtime" / f".tmp-{{bundle_sha}}-{{os.getpid()}}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(BUNDLE_B64)
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        zf.extractall(tmp_dir)
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        if runtime_dir.exists():
            return runtime_dir
        tmp_dir.rename(runtime_dir)
    except OSError:
        if not marker.is_file():
            with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                zf.extractall(runtime_dir)
    return runtime_dir


def main() -> int:
    t0 = time.time()
    cache_root = _resolve_cache_root()
    bundle_sha = SPEC["bundle_sha"]
    job_id = SPEC["job_id"]
    runtime_dir = _ensure_bundle_extracted(cache_root, bundle_sha)

    job_dir = Path(os.environ.get("NEXUS_LITE_JOB_DIR") or (cache_root / "jobs" / job_id)).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    ws_dir = job_dir / ".nexus"

    sys.path.insert(0, str(runtime_dir))
    prev_py = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = f"{{runtime_dir}}{{os.pathsep}}{{prev_py}}" if prev_py else str(runtime_dir)
    os.environ["NEXUS_LITE_WORKSPACE"] = str(ws_dir)

    for rel_path, content in (SPEC.get("files") or {{}}).items():
        dest = (job_dir / rel_path).resolve()
        if str(dest).startswith(str(job_dir)):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(content), encoding="utf-8")

    import nexus_lite
    nexus_lite.init(ws_dir)

    rc = 0
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    result_val = None
    tunnel_info = None

    prev_cwd = os.getcwd()
    try:
        os.chdir(str(job_dir))
        if SPEC.get("start_tunnel"):
            from nexus_lite.colab import tunnel as _tunnel
            tunnel_info = _tunnel.start_tunnel(port= int(SPEC.get("tunnel_port") or 8765))

        if SPEC.get("python") is not None:
            ns = {{
                "__name__": "__main__",
                "__file__": str(job_dir / "inline_task.py"),
                "nexus_lite": nexus_lite,
                "nl": nexus_lite,
                "WORKSPACE": ws_dir,
                "JOB_DIR": job_dir,
                "RESULT": None,
            }}
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                exec(compile(SPEC["python"], str(job_dir / "inline_task.py"), "exec"), ns, ns)
            result_val = ns.get("RESULT")
        elif SPEC.get("lite_argv") is not None:
            from nexus_lite import cli as _cli
            saved_argv = list(sys.argv)
            sys.argv = ["nexus-lite", *[str(a) for a in SPEC["lite_argv"]]]
            try:
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    try:
                        maybe_rc = _cli.main()
                        rc = int(maybe_rc or 0)
                    except SystemExit as exc:
                        rc = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            finally:
                sys.argv = saved_argv
        elif SPEC.get("cmd") is not None:
            proc = subprocess.run(
                SPEC["cmd"],
                shell=True,
                cwd=str(job_dir),
                env=dict(os.environ),
                stdin=subprocess.DEVNULL,
                 capture_output=True,
                text=True,
            )
            rc = proc.returncode
            out_buf.write(proc.stdout or "")
            err_buf.write(proc.stderr or "")
    except SystemExit as exc:
        rc = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception:
        rc = 1
        err_buf.write(traceback.format_exc())
    finally:
        try:
            os.chdir(prev_cwd)
        except OSError:
            pass

    collected: dict[str, str] = {{}}
    for rel_path in (SPEC.get("collect_files") or []):
        cand = (job_dir / rel_path).resolve()
        if str(cand).startswith(str(job_dir)) and cand.is_file():
            try:
                collected[rel_path] = cand.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

    payload = {{
        "ok": rc == 0,
        "status": "completed" if rc == 0 else "failed",
        "target": SPEC.get("target", "remote"),
        "mode": SPEC.get("mode", "direct"),
        "job_id": job_id,
        "bundle_sha": bundle_sha,
        "hostname": socket.gethostname(),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "slurm_nodelist": os.environ.get("SLURM_JOB_NODELIST") or os.environ.get("SLURMD_NODENAME"),
        "python_version": platform.python_version(),
        "lite_version": getattr(nexus_lite, "__version__", "unknown"),
        "runtime_dir": str(runtime_dir),
        "job_dir": str(job_dir),
        "workspace": str(ws_dir),
        "rc": rc,
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "result": result_val,
        "artifacts": collected,
        "tunnel": tunnel_info,
        "elapsed_s": round(time.time() - t0, 3),
    }}
    try:
        (job_dir / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    sys.stdout.write("\\n" + RESULT_MARKER + json.dumps(payload, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return script, bundle_sha, resolved_job_id


def parse_runner_output(stdout: str, stderr: str = "", fallback_rc: int = 0) -> dict[str, Any]:
    """Extract the structured JSON payload emitted after `RESULT_MARKER`."""
    for line in reversed((stdout or "").splitlines()):
        idx = line.find(RESULT_MARKER)
        if idx != -1:
            raw_json = line[idx + len(RESULT_MARKER) :].strip()
            try:
                data = json.loads(raw_json)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {
        "ok": fallback_rc == 0,
        "status": "completed" if fallback_rc == 0 else "failed",
        "rc": fallback_rc,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "result": None,
        "artifacts": {},
    }


def _run_ssh_stdin(
    host: str,
    remote_cmd: list[str],
    stdin_text: str,
    timeout: float = 300.0,
    jump_host: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run a remote command over SSH, with automatic fallback via `jump_host` if direct SSH fails (rc 255)."""
    base_ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    direct_argv = [*base_ssh, host, *remote_cmd]
    proc = subprocess.run(
        direct_argv,
        input=stdin_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode == 255 and jump_host and jump_host != host:
        inner = " ".join(shlex.quote(x) for x in ["ssh", "-o", "BatchMode=yes", host, *remote_cmd])
        jump_argv = [*base_ssh, jump_host, inner]
        return subprocess.run(
            jump_argv,
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    return proc


def run_on_turing(
    *,
    python: Optional[str] = None,
    cmd: Optional[str] = None,
    lite_argv: Optional[list[str]] = None,
    files: Optional[dict[str, str]] = None,
    collect_files: Optional[list[str]] = None,
    mode: str = "srun",
    partition: str = "batch",
    gres: Optional[str] = None,
    cpus: int = 2,
    mem: Optional[str] = None,
    time_limit: str = "00:10:00",
    ssh_host: Optional[str] = None,
    jump_host: Optional[str] = None,
    remote_python: str = "python3",
    job_id: Optional[str] = None,
    include_extras: bool = True,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Ship `nexus_lite` to an SSH / SLURM HPC cluster and execute a computation.

    Modes:
    - `mode="srun"` (default): Stages `nexus_lite` onto shared NFS (`~/.cache/nexus_lite`) on the login
      node, then executes the runner on a SLURM compute node via `srun`.
    - `mode="ssh"`: Runs directly on the SSH login node without SLURM allocation.
    - `mode="sbatch"`: Stages `nexus_lite` and submits a non-blocking SLURM batch job via `sbatch`,
      returning `slurm_job_id` and `job_dir`.
    """
    if mode not in ("srun", "ssh", "sbatch"):
        raise ValueError(f"Unsupported Turing mode {mode!r}; expected 'srun', 'ssh', or 'sbatch'")

    host = ssh_host or os.environ.get("NEXUS_TURING_HOST") or os.environ.get("NEXUS_HPC_HOST") or DEFAULT_TURING_HOST
    jhost = (
        jump_host
        if jump_host is not None
        else (os.environ.get("NEXUS_TURING_JUMP_HOST") or os.environ.get("NEXUS_HPC_JUMP_HOST") or DEFAULT_TURING_JUMP_HOST)
    )

    runner_src, bundle_sha, resolved_job_id = pack_self_contained_script(
        python=python,
        cmd=cmd,
        lite_argv=lite_argv,
        files=files,
        collect_files=collect_files,
        job_id=job_id,
        target="turing",
        mode=mode,
        include_extras=include_extras,
    )

    if mode == "ssh":
        proc = _run_ssh_stdin(host, [remote_python, "-"], runner_src, timeout=timeout, jump_host=jhost)
        res = parse_runner_output(proc.stdout, proc.stderr, proc.returncode)
        res.setdefault("target", "turing")
        res.setdefault("mode", "ssh")
        return res

    # For srun and sbatch, stage runner.py onto shared NFS (~/.cache/nexus_lite/jobs/<job_id>/runner.py)
    # on the login node, then invoke srun or sbatch.
    runner_b64 = base64.b64encode(runner_src.encode("utf-8")).decode("ascii")
    slurm_opts = {
        "mode": mode,
        "job_id": resolved_job_id,
        "bundle_sha": bundle_sha,
        "partition": partition,
        "gres": gres,
        "cpus": int(cpus),
        "mem": mem,
        "time_limit": time_limit,
        "remote_python": remote_python,
        "runner_b64": runner_b64,
    }
    stage_script = f"""
import base64, json, os, shlex, subprocess, sys
from pathlib import Path

opts = json.loads({json.dumps(slurm_opts)!r})
job_dir = (Path.home() / ".cache" / "nexus_lite" / "jobs" / opts["job_id"]).resolve()
job_dir.mkdir(parents=True, exist_ok=True)
runner_path = job_dir / "runner.py"
runner_path.write_bytes(base64.b64decode(opts["runner_b64"]))
runner_path.chmod(0o755)

if opts["mode"] == "srun":
    cmd = [
        "srun",
        f"--job-name={{opts['job_id']}}",
        f"--partition={{opts['partition']}}",
        "--ntasks=1",
        f"--cpus-per-task={{opts['cpus']}}",
        f"--time={{opts['time_limit']}}",
    ]
    if opts.get("gres"):
        cmd.append(f"--gres={{opts['gres']}}")
    if opts.get("mem"):
        cmd.append(f"--mem={{opts['mem']}}")
    cmd += [opts["remote_python"], str(runner_path)]
    proc = subprocess.run(cmd, cwd=str(job_dir), text=True, capture_output=True)
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    raise SystemExit(proc.returncode)
else:
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={{opts['job_id']}}",
        f"#SBATCH --partition={{opts['partition']}}",
        "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={{opts['cpus']}}",
        f"#SBATCH --time={{opts['time_limit']}}",
        f"#SBATCH --output={{job_dir / 'slurm.out'}}",
        f"#SBATCH --error={{job_dir / 'slurm.err'}}",
    ]
    if opts.get("gres"):
        lines.append(f"#SBATCH --gres={{opts['gres']}}")
    if opts.get("mem"):
        lines.append(f"#SBATCH --mem={{opts['mem']}}")
    lines.append(f"exec {{shlex.quote(opts['remote_python'])}} {{shlex.quote(str(runner_path))}}")
    sbatch_path = job_dir / "job.sbatch"
    sbatch_path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
    proc = subprocess.run(["sbatch", "--parsable", str(sbatch_path)], cwd=str(job_dir), text=True, capture_output=True)
    slurm_id = (proc.stdout or "").strip().split(";")[0]
    payload = {{
        "ok": proc.returncode == 0 and bool(slurm_id),
        "status": "submitted" if proc.returncode == 0 else "failed",
        "target": "turing",
        "mode": "sbatch",
        "job_id": opts["job_id"],
        "bundle_sha": opts["bundle_sha"],
        "slurm_job_id": slurm_id or None,
        "job_dir": str(job_dir),
        "rc": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }}
    sys.stdout.write("\\n" + {RESULT_MARKER!r} + json.dumps(payload) + "\\n")
    raise SystemExit(proc.returncode)
"""
    proc = _run_ssh_stdin(host, [remote_python, "-"], stage_script, timeout=timeout, jump_host=jhost)
    return parse_runner_output(proc.stdout, proc.stderr, proc.returncode)


def turing_job_status(
    job_id: str,
    *,
    slurm_job_id: Optional[str] = None,
    ssh_host: Optional[str] = None,
    jump_host: Optional[str] = None,
    remote_python: str = "python3",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Query the status and result of a `mode="sbatch"` job on an SSH / SLURM HPC cluster."""
    host = ssh_host or os.environ.get("NEXUS_TURING_HOST") or os.environ.get("NEXUS_HPC_HOST") or DEFAULT_TURING_HOST
    jhost = (
        jump_host
        if jump_host is not None
        else (os.environ.get("NEXUS_TURING_JUMP_HOST") or os.environ.get("NEXUS_HPC_JUMP_HOST") or DEFAULT_TURING_JUMP_HOST)
    )
    probe = f"""
import json, subprocess, sys
from pathlib import Path

job_id = {job_id!r}
slurm_id = {slurm_job_id!r}
job_dir = (Path.home() / ".cache" / "nexus_lite" / "jobs" / job_id).resolve()
res_file = job_dir / "result.json"
if res_file.is_file():
    data = json.loads(res_file.read_text(encoding="utf-8"))
    sys.stdout.write("\\n" + {RESULT_MARKER!r} + json.dumps(data) + "\\n")
    raise SystemExit(0)

state = "unknown"
if slurm_id:
    sq = subprocess.run(["squeue", "-h", "-j", str(slurm_id), "-o", "%T"], text=True, capture_output=True)
    state = (sq.stdout or "").strip() or "finished_or_not_in_queue"
payload = {{
    "ok": True,
    "status": state.lower(),
    "target": "turing",
    "mode": "sbatch",
    "job_id": job_id,
    "slurm_job_id": slurm_id,
    "job_dir": str(job_dir),
    "stdout": (job_dir / "slurm.out").read_text(encoding="utf-8", errors="replace") if (job_dir / "slurm.out").is_file() else "",
    "stderr": (job_dir / "slurm.err").read_text(encoding="utf-8", errors="replace") if (job_dir / "slurm.err").is_file() else "",
}}
sys.stdout.write("\\n" + {RESULT_MARKER!r} + json.dumps(payload) + "\\n")
"""
    proc = _run_ssh_stdin(host, [remote_python, "-"], probe, timeout=timeout, jump_host=jhost)
    return parse_runner_output(proc.stdout, proc.stderr, proc.returncode)


def _discover_colab_tunnel() -> tuple[str, str]:
    """Discover active Colab tunnel URL and token from env or `.nexus/tunnel.json`."""
    url = (os.environ.get("NEXUS_COLAB_URL") or "").strip().rstrip("/")
    token = (os.environ.get("NEXUS_COLAB_TOKEN") or "").strip()
    if url:
        return url, token
    for cand in (Path.cwd() / ".nexus" / "tunnel.json", Path("/content/.nexus/tunnel.json")):
        if cand.is_file():
            try:
                info = json.loads(cand.read_text(encoding="utf-8"))
                url = str(info.get("public_url") or info.get("local_url") or "").strip().rstrip("/")
                port = int(info.get("port") or 8765)
                if not token:
                    tok_file = Path(tempfile.gettempdir()) / f"nexus-lite-{os.getuid()}" / f"tunnel-{port}.token"
                    if tok_file.is_file():
                        token = tok_file.read_text(encoding="utf-8").strip()
                if url:
                    return url, token
            except Exception:
                pass
    return "", token


def run_on_colab(
    *,
    python: Optional[str] = None,
    cmd: Optional[str] = None,
    lite_argv: Optional[list[str]] = None,
    files: Optional[dict[str, str]] = None,
    collect_files: Optional[list[str]] = None,
    url: Optional[str] = None,
    token: Optional[str] = None,
    ssh_host: Optional[str] = None,
    bootstrap_out: Optional[str] = None,
    start_tunnel: bool = False,
    job_id: Optional[str] = None,
    include_extras: bool = True,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Ship `nexus_lite` to an external Google Colab runtime and execute a computation.

    Supports three zero-preinstall delivery modes:
    1. `url` (or `NEXUS_COLAB_URL` / `.nexus/tunnel.json`): Sends the self-extracting `nexus_lite`
       bundle over `POST <url>/exec` (`{"python": runner_src}`) and returns the parsed JSON result.
    2. `ssh_host` (or `NEXUS_COLAB_SSH`): Streams the self-extracting `nexus_lite` bundle over
       `ssh <ssh_host> python3 -`.
    3. `bootstrap_out` (or when no tunnel/SSH is configured): Writes a single self-contained `.py`
       file (e.g. on Google Drive or local disk) that any bare Colab notebook can run via
       `!python3 <file.py>` with zero git clone and zero pip install.
    """
    resolved_url, resolved_token = _discover_colab_tunnel()
    if url:
        resolved_url = url.strip().rstrip("/")
    if token is not None:
        resolved_token = token.strip()
    resolved_ssh = (ssh_host or os.environ.get("NEXUS_COLAB_SSH") or "").strip()

    mode = "tunnel" if resolved_url else ("ssh" if resolved_ssh else "bootstrap")
    runner_src, bundle_sha, resolved_job_id = pack_self_contained_script(
        python=python,
        cmd=cmd,
        lite_argv=lite_argv,
        files=files,
        collect_files=collect_files,
        job_id=job_id,
        target="colab",
        mode=mode,
        include_extras=include_extras,
        start_tunnel=start_tunnel,
    )

    if bootstrap_out:
        out_path = Path(bootstrap_out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(runner_src, encoding="utf-8")
        if not resolved_url and not resolved_ssh:
            return {
                "ok": True,
                "status": "bootstrapped",
                "target": "colab",
                "mode": "bootstrap",
                "job_id": resolved_job_id,
                "bundle_sha": bundle_sha,
                "script_path": str(out_path),
                "colab_command": f"!python3 {shlex.quote(str(out_path))}",
                "rc": 0,
                "stdout": f"Wrote self-contained Colab bundle ({len(runner_src)} bytes) to {out_path}",
                "stderr": "",
            }

    if resolved_url:
        headers = {"Content-Type": "application/json"}
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"

        def _post_exec(py_snippet: str) -> dict[str, Any]:
            body = json.dumps({"python": py_snippet, "timeout": timeout}).encode("utf-8")
            req = urllib.request.Request(f"{resolved_url}/exec", data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        # Linux enforces MAX_ARG_STRLEN = 131072 bytes on each argv element passed to `python -c`.
        # Stage large self-extracting runners in <=48 KB chunks to a remote temp file before running.
        if len(runner_src.encode("utf-8")) <= 48000:
            raw = _post_exec(runner_src)
        else:
            b64_full = base64.b64encode(runner_src.encode("utf-8")).decode("ascii")
            chunk_sz = 48000
            chunks = [b64_full[i : i + chunk_sz] for i in range(0, len(b64_full), chunk_sz)]
            for idx, chk in enumerate(chunks):
                fmode = "w" if idx == 0 else "a"
                stage_py = (
                    "import pathlib, tempfile; "
                    f"p = pathlib.Path(tempfile.gettempdir()) / {f'nexus_colab_{resolved_job_id}.b64'!r}; "
                    f"open(p, {fmode!r}, encoding='ascii').write({chk!r})"
                )
                _post_exec(stage_py)
            run_py = (
                "import base64, pathlib, subprocess, sys, tempfile\n"
                f"b64_p = pathlib.Path(tempfile.gettempdir()) / {f'nexus_colab_{resolved_job_id}.b64'!r}\n"
                f"py_p = pathlib.Path(tempfile.gettempdir()) / {f'nexus_colab_{resolved_job_id}.py'!r}\n"
                "try:\n"
                "    py_p.write_bytes(base64.b64decode(b64_p.read_text(encoding='ascii')))\n"
                "    proc = subprocess.run([sys.executable, str(py_p)], text=True, capture_output=True)\n"
                "    sys.stdout.write(proc.stdout or '')\n"
                "    sys.stderr.write(proc.stderr or '')\n"
                "    raise SystemExit(proc.returncode)\n"
                "finally:\n"
                "    b64_p.unlink(missing_ok=True)\n"
                "    py_p.unlink(missing_ok=True)\n"
            )
            raw = _post_exec(run_py)

        out = str(raw.get("stdout") or "")
        err = str(raw.get("stderr") or "")
        rc = int(raw.get("rc") if raw.get("rc") is not None else 1)
        return parse_runner_output(out, err, rc)

    if resolved_ssh:
        proc = _run_ssh_stdin(resolved_ssh, ["python3", "-"], runner_src, timeout=timeout, jump_host=None)
        return parse_runner_output(proc.stdout, proc.stderr, proc.returncode)

    # Default fallback when no URL/SSH was given and bootstrap_out was not set:
    default_out = (Path.cwd() / ".nexus" / "colab_bundles" / f"{resolved_job_id}.py").resolve()
    default_out.parent.mkdir(parents=True, exist_ok=True)
    default_out.write_text(runner_src, encoding="utf-8")
    return {
        "ok": True,
        "status": "bootstrapped",
        "target": "colab",
        "mode": "bootstrap",
        "job_id": resolved_job_id,
        "bundle_sha": bundle_sha,
        "script_path": str(default_out),
        "colab_command": f"!python3 {shlex.quote(str(default_out))}",
        "rc": 0,
        "stdout": f"Generated self-contained Colab script ({len(runner_src)} bytes) at {default_out}",
        "stderr": "",
    }


def run_local_sandbox(
    *,
    python: Optional[str] = None,
    cmd: Optional[str] = None,
    lite_argv: Optional[list[str]] = None,
    files: Optional[dict[str, str]] = None,
    collect_files: Optional[list[str]] = None,
    job_id: Optional[str] = None,
    include_extras: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run the self-extracting `nexus_lite` bundle in a clean local temporary directory (`-I` isolated mode)."""
    runner_src, _sha, _jid = pack_self_contained_script(
        python=python,
        cmd=cmd,
        lite_argv=lite_argv,
        files=files,
        collect_files=collect_files,
        job_id=job_id,
        target="local",
        mode="sandbox",
        include_extras=include_extras,
    )
    with tempfile.TemporaryDirectory(prefix="nexus_lite_sandbox_") as td:
        env = dict(os.environ)
        env["NEXUS_LITE_RUNTIME_ROOT"] = td
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [sys.executable, "-I", "-"],
            input=runner_src,
            text=True,
            capture_output=True,
            cwd=td,
            env=env,
            timeout=timeout,
        )
        return parse_runner_output(proc.stdout, proc.stderr, proc.returncode)


def compute_run(
    target: str = "turing",
    *,
    python: Optional[str] = None,
    cmd: Optional[str] = None,
    lite_argv: Optional[list[str]] = None,
    files: Optional[dict[str, str]] = None,
    collect_files: Optional[list[str]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Unified dispatcher for `turing`/`hpc` (internal SLURM HPC), `colab` (external compute), or `local` (sandbox)."""
    tgt = (target or "turing").strip().lower()
    if tgt in ("turing", "hpc"):
        return run_on_turing(
            python=python,
            cmd=cmd,
            lite_argv=lite_argv,
            files=files,
            collect_files=collect_files,
            **kwargs,
        )
    if tgt == "colab":
        return run_on_colab(
            python=python,
            cmd=cmd,
            lite_argv=lite_argv,
            files=files,
            collect_files=collect_files,
            **kwargs,
        )
    if tgt == "local":
        return run_local_sandbox(
            python=python,
            cmd=cmd,
            lite_argv=lite_argv,
            files=files,
            collect_files=collect_files,
            **kwargs,
        )
    raise ValueError(f"Unknown compute target {target!r}; expected 'turing', 'hpc', 'colab', or 'local'")
