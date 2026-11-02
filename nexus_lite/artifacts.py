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


def artifacts_root(workspace: PathLike) -> Path:
    """Default artifact root: <workspace>/artifacts (no env override)."""
    return Path(workspace) / "artifacts"


def task_dir(task_id: str, root: PathLike) -> Path:
    """Pure path: <root>/<task_id>."""
    return Path(root) / task_id


def ensure_task_dir(task_id: str, root: PathLike) -> Path:
    d = task_dir(task_id, root)
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    (d / OUTPUT).write_text(output, encoding="utf-8")
    (d / RESULT).write_text(output, encoding="utf-8")
    (d / RC).write_text(str(rc), encoding="utf-8")


def write_meta(art_dir: PathLike, *, task_id: str, agent: str, status: str,
               rc: int, started: float, finished: float) -> None:
    (Path(art_dir) / META).write_text(
        json.dumps({"task_id": task_id, "agent": agent, "status": status,
                    "rc": rc, "started": started, "finished": finished}),
        encoding="utf-8")
