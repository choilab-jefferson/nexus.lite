"""Machine-local artifact location registry (stdlib-only).

Maps content (sha256) and logical ``artifact_id`` to relative paths. Lite only records and
looks up; resolving to absolute paths, sync, mirroring and epoch refresh are nexus-side.

Default file ``<ws>/cas/ARTIFACT_LOCATIONS.json`` (the workspace ``.nexus/`` is gitignored, so
it is never tracked); pass ``path=`` to place it elsewhere (caller gitignores it).
Doc: ``{"schema":1,"by_sha256":{sha:{label:[relpath]}},
"by_artifact_id":{aid:{"sha256":sha,"locations":{label:relpath}}}}``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

from . import cas
from .artifacts import mkdir_private, write_private

SCHEMA = 1
DEFAULT_LABEL = "workspace"


def _path(ws, path) -> Path:
    return Path(path) if path is not None else cas._ws(ws) / "cas" / "ARTIFACT_LOCATIONS.json"


def _empty() -> Dict[str, object]:
    return {"schema": SCHEMA, "by_sha256": {}, "by_artifact_id": {}}


def load(*, ws=None, path=None) -> Dict[str, object]:
    """Full registry doc; an empty skeleton when the file is missing or corrupt."""
    try:
        doc = json.loads(_path(ws, path).read_text("utf-8"))
        if (doc.get("schema") == SCHEMA and isinstance(doc.get("by_sha256"), dict)
                and isinstance(doc.get("by_artifact_id"), dict)):
            return doc
    except (OSError, ValueError, AttributeError):
        pass
    return _empty()


def _save(doc, ws, path) -> None:
    p = _path(ws, path)
    if path is None:
        cas._mkdirs(cas._ws(ws), p.parent)
    else:
        mkdir_private(p.parent)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        write_private(tmp, json.dumps(doc, sort_keys=True))
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def _check_rel(relpath: str) -> str:
    pp = PurePosixPath(relpath)
    if not relpath or pp.is_absolute() or "\\" in relpath or ".." in pp.parts:
        raise ValueError(f"bad relpath (want relative posix, no '..'): {relpath!r}")
    return pp.as_posix()


def _drop(doc, sha: str, label: str, rel: str) -> None:
    """Remove ``rel`` from by_sha256[sha][label], pruning empty containers."""
    labels = doc["by_sha256"].get(sha, {})
    lst = [x for x in labels.get(label, []) if x != rel]
    if lst:
        labels[label] = lst
    else:
        labels.pop(label, None)
    if not labels:
        doc["by_sha256"].pop(sha, None)


def _apply(doc, aid: str, sha: str, rel: str, label: str) -> None:
    cas.check_artifact_id(aid)
    cas.blob_path(sha)
    sha, rel = sha.lower(), _check_rel(rel)
    cur = doc["by_artifact_id"].get(aid)
    if cur and cur["sha256"] != sha:
        for lb, old in cur["locations"].items():
            _drop(doc, cur["sha256"], lb, old)
        cur = None
    if cur is None:
        cur = doc["by_artifact_id"][aid] = {"sha256": sha, "locations": {}}
    old = cur["locations"].get(label)
    if old is not None and old != rel:
        _drop(doc, sha, label, old)
    cur["locations"][label] = rel
    lst = doc["by_sha256"].setdefault(sha, {}).setdefault(label, [])
    if rel not in lst:
        lst.append(rel)
        lst.sort()


def record_many(items: Iterable[Tuple[str, str, str]], *, label: str = DEFAULT_LABEL,
                ws=None, path=None) -> None:
    """Record (artifact_id, sha256, relpath) triples with one read and one atomic write."""
    doc = load(ws=ws, path=path)
    for aid, sha, rel in items:
        _apply(doc, aid, sha, rel, label)
    _save(doc, ws, path)


def record(artifact_id: str, sha256: str, relpath: str, *, label: str = DEFAULT_LABEL,
           ws=None, path=None) -> None:
    """Record that ``artifact_id`` (content ``sha256``) lives at ``relpath`` under ``label``.

    Re-recording an id with a different sha replaces its sha and prunes the stale entries.
    """
    record_many([(artifact_id, sha256, relpath)], label=label, ws=ws, path=path)


def by_sha256(sha256: str, *, ws=None, path=None) -> Dict[str, List[str]]:
    """{label: [relpath, ...]} for content ``sha256`` ({} if unknown)."""
    return {k: list(v) for k, v in load(ws=ws, path=path)["by_sha256"].get(sha256.lower(), {}).items()}


def by_artifact_id(artifact_id: str, *, ws=None, path=None) -> Optional[Dict[str, object]]:
    """{"sha256", "locations": {label: relpath}} or None."""
    e = load(ws=ws, path=path)["by_artifact_id"].get(artifact_id)
    return {"sha256": e["sha256"], "locations": dict(e["locations"])} if e else None


def forget(artifact_id: str, *, label: Optional[str] = None, ws=None, path=None) -> None:
    """Drop one label (or the whole id when ``label`` is None) from the registry."""
    doc = load(ws=ws, path=path)
    cur = doc["by_artifact_id"].get(artifact_id)
    if not cur:
        return
    for lb in ([label] if label else list(cur["locations"])):
        rel = cur["locations"].pop(lb, None)
        if rel is not None:
            _drop(doc, cur["sha256"], lb, rel)
    if not cur["locations"]:
        doc["by_artifact_id"].pop(artifact_id)
    _save(doc, ws, path)
