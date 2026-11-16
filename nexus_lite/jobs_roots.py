"""The roots index of ``nexus-lite job`` (parity with xrun's ``.xrun-roots``, #1151): a small file that lists every
job root a ``job start`` used, so ``job list --all`` and ``job status NAME`` find runs started from other projects,
worktrees or workspaces.

Format: one absolute root per line, de-duplicated, first occurrence first - the same file xrun writes, so both tools
share one index. Path: ``$NEXUS_LITE_ROOTS_INDEX``, else ``$XRUN_ROOTS_INDEX``, else
``~/.local/share/nexus/artifacts/.xrun-roots``. It holds paths only (no job names, commands or logs), so it carries no
PHI. Writes replace the file by rename under an exclusive flock on ``<index>.lock``: a reader never sees a torn file and
concurrent starts never duplicate a line. Adding is never fatal: a failure returns False and the job still starts.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - not on POSIX
    fcntl = None


def index_path() -> Optional[Path]:
    env = os.environ.get("NEXUS_LITE_ROOTS_INDEX") or os.environ.get("XRUN_ROOTS_INDEX")
    if env:
        return Path(env)
    try:
        return Path.home() / ".local" / "share" / "nexus" / "artifacts" / ".xrun-roots"
    except RuntimeError:  # no home directory
        return None


def _clean(lines: List[str]) -> List[str]:
    seen, out = set(), []
    for ln in lines:
        ln = ln.strip()
        if ln and "\0" not in ln and ln not in seen:  # a NUL makes os.path calls raise: skip the torn line
            seen.add(ln)
            out.append(ln)
    return out


def indexed_roots() -> List[str]:
    idx = index_path()
    try:
        return _clean(idx.read_text(errors="replace").splitlines()) if idx else []
    except OSError:
        return []


def _edit(fn: Callable[[List[str]], List[str]]) -> bool:
    """Rewrite the index through fn(lines) -> lines, under the flock, by temp file + rename. True if it changed."""
    idx = index_path()
    if idx is None:
        return False
    idx.parent.mkdir(parents=True, exist_ok=True)
    with open(str(idx) + ".lock", "a") as lk:
        if fcntl:
            fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            lines = idx.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        out = fn(lines)
        if out == lines:
            return False
        fd, tmp = tempfile.mkstemp(prefix=idx.name + ".", dir=str(idx.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("".join(ln + "\n" for ln in out))
            os.replace(tmp, idx)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True


def add(root) -> bool:
    """Record `root` once. Never raises; False if it could not be recorded."""
    r = str(root)
    if r in indexed_roots():
        return True
    try:
        _edit(lambda lines: _clean(lines + [r]))
        return True
    except OSError:
        return False


def prune() -> List[str]:
    """Drop the roots whose directory is gone (never touches run dirs). Returns the dropped roots."""
    dropped: List[str] = []

    def fn(lines: List[str]) -> List[str]:
        keep = []
        for r in _clean(lines):
            (keep if os.path.isdir(r) else dropped).append(r)
        return keep
    try:
        _edit(fn)
    except OSError:
        return []
    return dropped
