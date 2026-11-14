"""Task-artifact layout contract (stdlib only, no nexus_lite imports).

One per-task directory holds every artifact of a delegated task:

    <root>/<task_id>/
        prompt.md   input prompt           output.md  worker stdout (lite name)
        result.md   same bytes as output.md (code-style readers)
        meta.json   {task_id, agent, status, rc, started, finished}
        rc          exit code as text      spec.json  bg runner spec
        (timeout, runner.pid, runner.log, stderr.log, escalation.json, ...)

Root: ``<workspace>/artifacts``. The root is an argument; a downstream
multi-project host passes its own root to ``task_dir``/``ensure_task_dir``.

Env exported to workers (one release of overlap):
    NEXUS_TASK_DIR      canonical per-task directory
    NEXUS_ARTIFACT_DIR  deprecated alias, same value
Readers use ``task_dir_from_env``: canonical wins, alias accepted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Optional, Union

PROMPT = "prompt.md"
OUTPUT = "output.md"
RESULT = "result.md"
META = "meta.json"
RC = "rc"
SPEC = "spec.json"

TASK_DIR_ENV = "NEXUS_TASK_DIR"
TASK_DIR_ENV_DEPRECATED = "NEXUS_ARTIFACT_DIR"

PathLike = Union[str, "os.PathLike[str]"]


def mkdir_private(path: PathLike) -> Path:
    """Create `path` (and parents) and force the leaf to 0700. Best-effort chmod: a dir we do not own is left alone."""
    p = Path(path)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(str(p), 0o700)
    except OSError:
        pass
    return p


def write_private(path: PathLike, data: Union[str, bytes], *, append: bool = False) -> Path:
    """Write `data` to `path` as a 0600 file (created 0600, tightened if it already existed)."""
    p = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(p), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "ab" if append else "wb") as f:
            fd = -1
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    finally:
        if fd >= 0:
            os.close(fd)
    return p


def replace_private(path: PathLike, data: Union[str, bytes]) -> Path:
    """Publish `data` at `path` atomically as a 0600 file: write a temp file in the same directory, then
    os.replace it into place. Readers see either no file or the complete body, never a partial one."""
    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        write_private(tmp, data)  # O_CREAT|O_TRUNC|O_NOFOLLOW, 0600
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return p


def artifacts_root(workspace: PathLike) -> Path:
    """Default artifact root: <workspace>/artifacts (no env override)."""
    return Path(workspace) / "artifacts"


def task_dir(task_id: str, root: PathLike) -> Path:
    """Pure path: <root>/<task_id>."""
    return Path(root) / task_id


def ensure_task_dir(task_id: str, root: PathLike) -> Path:
    return mkdir_private(task_dir(task_id, root))


def task_dir_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Per-task dir from env: canonical name wins, deprecated alias accepted."""
    e = os.environ if env is None else env
    v = e.get(TASK_DIR_ENV) or e.get(TASK_DIR_ENV_DEPRECATED)
    return Path(v) if v else None


def export_env(env: dict, art_dir: PathLike) -> dict:
    """Set canonical and deprecated per-task dir vars on ``env`` (in place)."""
    env[TASK_DIR_ENV] = str(art_dir)
    env[TASK_DIR_ENV_DEPRECATED] = str(art_dir)
    return env


def write_result(art_dir: PathLike, output: str, rc: int) -> None:
    """Write output.md, result.md (same content) and rc."""
    d = Path(art_dir)
    write_private(d / OUTPUT, output)
    write_private(d / RESULT, output)
    write_private(d / RC, str(rc))


def write_meta(art_dir: PathLike, *, task_id: str, agent: str, status: str,
               rc: int, started: float, finished: float) -> None:
    write_private(
        Path(art_dir) / META,
        json.dumps({"task_id": task_id, "agent": agent, "status": status,
                    "rc": rc, "started": started, "finished": finished}))
