"""Stdlib-only content-addressed store (slice 1).

``<ws>`` is the lite workspace (``_db.get_workspace()``, ``<root>/.nexus`` by default).
Blobs live at ``<ws>/cas/sha256/<2 hex>/<62 hex>``, written atomically as 0600
files via ``artifacts.write_private`` (temp file in the same dir, sha256 re-verified, then
``os.replace``). Putting content that is already stored is a no-op: the blob is not
rewritten and the ``cas_objects`` row is not duplicated.

``manifest(dir)`` hashes every regular file under ``dir`` (stores each as a blob) and stores
the canonical JSON manifest itself as a blob; its sha256 is the manifest id.

PHI: lite's gate (``phi.py``) only refuses dispatch of non-local agent commands; it does not
gate artifact writes. The CAS is local-disk only (no network, no dispatch), so there is
nothing to gate here. A remote push (later slice) must apply ``phi_gate`` itself.

Registration in ``cas_objects`` (lite.db) happens only for the current workspace
(``_db.get_workspace()``); a store at another ``ws`` is blob-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple, Union

from . import _db
from .artifacts import mkdir_private, write_private

PathLike = Union[str, "os.PathLike[str]"]
_CHUNK = 1 << 20
_REF_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DERIVED_EXTENSIONS = (".pdf", ".docx", ".pptx", ".html", ".svg", ".png")


def _ext_set(exclude_ext: Optional[Iterable[str]]) -> frozenset:
    """Normalise extensions to lowercase with a leading dot."""
    return frozenset("." + e.lower().lstrip(".") for e in exclude_ext or () if e.strip("."))


def _ws(ws: Optional[PathLike]) -> Path:
    return Path(ws).resolve() if ws is not None else _db.get_workspace()


def blob_path(sha: str, ws: Optional[PathLike] = None) -> Path:
    """Pure path of the blob for ``sha`` (validated 64-hex)."""
    sha = sha.lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"not a sha256 hex digest: {sha!r}")
    return _ws(ws) / "cas" / "sha256" / sha[:2] / sha[2:]


def has(sha: str, ws: Optional[PathLike] = None) -> bool:
    return blob_path(sha, ws).is_file()


def get_path(sha: str, ws: Optional[PathLike] = None) -> Path:
    p = blob_path(sha, ws)
    if not p.is_file():
        raise KeyError(sha)
    return p


class CorruptBlobError(ValueError):
    """A stored blob no longer hashes to its address."""


def get(sha: str, ws: Optional[PathLike] = None) -> bytes:
    data = get_path(sha, ws).read_bytes()
    if hashlib.sha256(data).hexdigest() != sha.lower():
        raise CorruptBlobError(f"cas: blob {sha} is corrupt")
    return data


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _register(sha: str, size: int, task_id: Optional[str]) -> None:
    with _db.connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO cas_objects (sha256, size, created_at, first_task_id) "
            "VALUES (?, ?, ?, ?);", (sha, size, time.time(), task_id))


def _mkdirs(root: Path, dest_dir: Path) -> None:
    """Create ``root`` and every dir down to ``dest_dir`` as 0700 (no umask-default parents)."""
    mkdir_private(root)
    cur = root
    for part in dest_dir.relative_to(root).parts:
        cur = mkdir_private(cur / part)


def put(src: Union[bytes, PathLike], ws: Optional[PathLike] = None, *,
        task_id: Optional[str] = None) -> str:
    """Store bytes or a file (streamed); return its sha256 hex.

    A blob that already hashes correctly is never rewritten; a corrupt one is replaced.
    """
    root = _ws(ws)
    registered = root == _db.get_workspace()
    if registered:
        _db.init()  # cas_objects must exist even on a never-initialised workspace
    _mkdirs(root, root / "cas")
    tmp = root / "cas" / f".put.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        if isinstance(src, (bytes, bytearray)):
            data = bytes(src)
            write_private(tmp, data)
            sha, size = hashlib.sha256(data).hexdigest(), len(data)
        else:
            h, size = hashlib.sha256(), 0
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    h.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
            sha = h.hexdigest()
        dest = blob_path(sha, root)
        if not (dest.is_file() and _hash_file(dest) == sha):
            _mkdirs(root, dest.parent)
            os.replace(tmp, dest)
            os.chmod(dest, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()
    if registered:
        _register(sha, size, task_id)
    return sha


MANIFEST_NAME = "ARTIFACT_MANIFEST.json"
MANIFEST_SCHEMA = 1
Entries = Dict[str, Dict[str, object]]


def _canon(entries: Entries, provenance: Optional[Dict[str, object]] = None) -> bytes:
    """Canonical manifest bytes; ``provenance`` is part of the hashed content."""
    doc: Dict[str, object] = {"schema": MANIFEST_SCHEMA, "files": entries}
    if provenance is not None:
        doc["provenance"] = provenance
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git_commit() -> Optional[str]:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


def build_provenance(*, task_id: Optional[str] = None, inputs: Optional[list] = None,
                     parent: Optional[str] = None, created_at: Optional[str] = None,
                     commit: Optional[str] = None, producer: Optional[Dict[str, str]] = None
                     ) -> Dict[str, object]:
    """Provenance block: pass every field for a fully deterministic result.

    Unset ``task_id``/``commit``/``producer``/``created_at`` fall back to ``NEXUS_TASK_ID``,
    ``git rev-parse HEAD``, ``NEXUS_AGENT``/``NEXUS_PROVIDER``/``NEXUS_MODEL`` (when set) and
    the current UTC time. Holds identifiers and hashes only.
    """
    env = os.environ
    if producer is None:
        producer = {k: env[v] for k, v in
                    (("agent", "NEXUS_AGENT"), ("provider", "NEXUS_PROVIDER"), ("model", "NEXUS_MODEL"))
                    if env.get(v)}
    return {
        "task_id": task_id or env.get("NEXUS_TASK_ID") or None,
        "commit": commit if commit is not None else _git_commit(),
        "producer": producer,
        "created_at": created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": sorted(inputs or []),
        "parent": parent,
    }


def read_manifest(sha: str, ws: Optional[PathLike] = None) -> Dict[str, object]:
    """Load a stored manifest doc by sha ({schema, files, provenance?})."""
    return json.loads(get(sha, ws))


# ---- refs: named mutable pointers to immutable manifest shas ----
# Layout ``<ws>/cas/refs/<namespace>/<name>`` (one small 0600 file holding the sha). A future
# remote backend only has to mirror blobs + refs; nothing here depends on how they are stored.
def _ref_path(name: str, ws: Optional[PathLike]) -> Path:
    parts = name.split("/")
    if len(parts) < 2 or not all(_REF_PART.match(x) and x not in (".", "..") for x in parts):
        raise ValueError(f"bad ref name (want <namespace>/<name>): {name!r}")
    return _ws(ws) / "cas" / "refs" / Path(*parts)


def ref_set(name: str, sha: str, ws: Optional[PathLike] = None) -> None:
    """Point ``name`` at manifest ``sha`` (atomic: temp file + ``os.replace``)."""
    blob_path(sha, ws)  # validates the digest
    p = _ref_path(name, ws)
    root = _ws(ws)
    _mkdirs(root, p.parent)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        write_private(tmp, sha.lower() + "\n")
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def ref_get(name: str, ws: Optional[PathLike] = None) -> Optional[str]:
    try:
        sha = _ref_path(name, ws).read_text("utf-8").strip()
    except OSError:
        return None
    return sha if len(sha) == 64 else None


def ref_list(namespace: str = "", ws: Optional[PathLike] = None) -> Dict[str, str]:
    """{ref name: sha} for every ref under ``namespace`` (all when empty)."""
    base = _ws(ws) / "cas" / "refs"
    out: Dict[str, str] = {}
    if not base.is_dir():
        return out
    for p in sorted(base.rglob("*")):
        name = p.relative_to(base).as_posix()
        if p.is_file() and not p.name.startswith(".") and (not namespace or name.startswith(namespace.strip("/") + "/")):
            sha = ref_get(name, ws)
            if sha:
                out[name] = sha
    return out


def dir_ref(directory: PathLike) -> str:
    """Ref the ``artifacts`` verbs use for a working dir (its HEAD)."""
    return "dirs/" + hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:16]


def put_manifest(entries: Entries, ws: Optional[PathLike] = None, *,
                 task_id: Optional[str] = None,
                 provenance: Optional[Dict[str, object]] = None) -> str:
    """Store already-hashed ``entries`` as a canonical manifest blob; return its sha256."""
    return put(_canon(entries, provenance), ws, task_id=task_id)


def write_manifest(directory: PathLike, entries: Entries, ws: Optional[PathLike] = None, *,
                   task_id: Optional[str] = None, provenance: Optional[Dict[str, object]] = None,
                   ref: Optional[str] = None) -> str:
    """Write ``<directory>/ARTIFACT_MANIFEST.json`` (0600, canonical JSON), store it in the CAS,
    and move ``ref`` (if given) to it."""
    sha = put_manifest(entries, ws, task_id=task_id, provenance=provenance)
    write_private(Path(directory) / MANIFEST_NAME, _canon(entries, provenance))
    if ref:
        ref_set(ref, sha, ws)
    return sha


def manifest(directory: PathLike, ws: Optional[PathLike] = None, *,
             task_id: Optional[str] = None, exclude_ext: Optional[Iterable[str]] = None) -> Tuple[Dict[str, Dict[str, object]], str]:
    """Store every regular file under ``directory``; return (manifest, manifest sha).

    Manifest = {relative posix path: {"sha256", "size"}}; symlinks are skipped. The canonical
    JSON (sorted keys) is itself stored as a CAS blob, so the same tree always yields one id.
    Files whose suffix is in ``exclude_ext`` (e.g. ``DERIVED_EXTENSIONS``) are skipped.
    """
    base = Path(directory)
    if not base.is_dir():
        raise FileNotFoundError(f"cas: not a directory: {directory}")
    entries: Dict[str, Dict[str, object]] = {}
    for rel, p in _files(base, exclude_ext):
        entries[rel] = {"sha256": put(p, ws, task_id=task_id), "size": p.stat().st_size}
    return entries, put_manifest(entries, ws, task_id=task_id)


# ---- logical manifest (schema 2): path-free, keyed by artifact_id ----
# Locations (paths) live in ``locations.py``; this manifest holds identity + content only.
LOGICAL_SCHEMA = 2
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(/[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$")
_LOGICAL_KEYS = ("sha256", "size_bytes", "kind", "format", "epoch_id", "producer_role")


def check_artifact_id(aid: str) -> str:
    """Validate a slash-separated artifact id such as ``p-1/iter_005/manuscript/final``."""
    if not isinstance(aid, str) or not _ARTIFACT_ID.match(aid) or ".." in aid.split("/"):
        raise ValueError(f"bad artifact_id: {aid!r}")
    return aid


def logical_entry(sha256: str, size_bytes: int, *, kind: Optional[str] = None,
                  format: Optional[str] = None, epoch_id: Optional[str] = None,
                  producer_role: Optional[str] = None) -> Dict[str, object]:
    """One logical-manifest entry; all six keys always present (``None`` allowed)."""
    blob_path(sha256)  # validates 64 hex
    return {"sha256": sha256.lower(), "size_bytes": int(size_bytes), "kind": kind,
            "format": format, "epoch_id": epoch_id, "producer_role": producer_role}


def _canon_logical(artifacts: Dict[str, Dict[str, object]],
                   provenance: Optional[Dict[str, object]] = None) -> bytes:
    for aid, e in artifacts.items():
        check_artifact_id(aid)
        if set(e) != set(_LOGICAL_KEYS):
            raise ValueError(f"bad logical entry for {aid!r}: keys {sorted(e)}")
        blob_path(str(e["sha256"]))
    doc: Dict[str, object] = {"schema": LOGICAL_SCHEMA, "artifacts": artifacts}
    if provenance is not None:
        doc["provenance"] = provenance
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def put_logical_manifest(artifacts: Dict[str, Dict[str, object]], ws: Optional[PathLike] = None, *,
                         provenance: Optional[Dict[str, object]] = None,
                         task_id: Optional[str] = None) -> str:
    """Store a path-free logical manifest blob; return its sha256."""
    return put(_canon_logical(artifacts, provenance), ws, task_id=task_id)


def write_logical_manifest(directory: PathLike, artifacts: Dict[str, Dict[str, object]],
                           ws: Optional[PathLike] = None, *,
                           provenance: Optional[Dict[str, object]] = None,
                           ref: Optional[str] = None, task_id: Optional[str] = None) -> str:
    """Write ``<directory>/ARTIFACT_MANIFEST.json`` (schema 2), store it, optionally move ``ref``."""
    data = _canon_logical(artifacts, provenance)
    sha = put(data, ws, task_id=task_id)
    write_private(Path(directory) / MANIFEST_NAME, data)
    if ref:
        ref_set(ref, sha, ws)
    return sha


def logical_manifest(directory: PathLike, artifact_id: Optional[Callable[[str], str]] = None,
                     ws: Optional[PathLike] = None, *,
                     meta: Optional[Callable[[str], Dict[str, object]]] = None,
                     exclude_ext: Optional[Iterable[str]] = DERIVED_EXTENSIONS,
                     task_id: Optional[str] = None,
                     provenance: Optional[Dict[str, object]] = None
                     ) -> Tuple[Dict[str, Dict[str, object]], str]:
    """Store every file under ``directory`` as a blob; return ({artifact_id: entry}, manifest sha).

    ``artifact_id(rel)`` maps a relative posix path to an id (default: path minus suffix);
    ``meta(rel)`` may return ``kind``/``epoch_id``/``producer_role``. Derived extensions are
    excluded by default. Paths are not recorded; use ``locations.record`` for that.
    """
    base = Path(directory)
    if not base.is_dir():
        raise FileNotFoundError(f"cas: not a directory: {directory}")
    to_id = artifact_id or (lambda rel: rel[: -len(Path(rel).suffix)] if Path(rel).suffix else rel)
    arts: Dict[str, Dict[str, object]] = {}
    for rel, p in _files(base, exclude_ext):
        aid = check_artifact_id(to_id(rel))
        if aid in arts:
            raise ValueError(f"duplicate artifact_id: {aid!r}")
        extra = dict(meta(rel)) if meta else {}
        extra.setdefault("format", p.suffix.lower().lstrip(".") or None)
        arts[aid] = logical_entry(put(p, ws, task_id=task_id), p.stat().st_size, **extra)
    return arts, put_logical_manifest(arts, ws, provenance=provenance, task_id=task_id)


# ---- offline verbs: add / commit / status / diff (slice 2) ----
def _index_path(ws: Optional[PathLike]) -> Path:
    return _ws(ws) / ".nexus" / "cas" / "index.json"


def _load_index(ws: Optional[PathLike]) -> Dict[str, Dict[str, str]]:
    try:
        return json.loads(_index_path(ws).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(ws: Optional[PathLike], idx: Dict[str, Dict[str, str]]) -> None:
    p = _index_path(ws)
    mkdir_private(p.parent)
    write_private(p, json.dumps(idx, sort_keys=True))


def _files(base: Path, exclude_ext: Optional[Iterable[str]] = None):
    skip = _ext_set(exclude_ext)
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base).as_posix()
        if (not p.is_symlink() and p.is_file() and rel != MANIFEST_NAME
                and p.suffix.lower() not in skip):
            yield rel, p


def last_manifest(directory: PathLike, ws: Optional[PathLike] = None) -> Dict[str, str]:
    """{rel: sha256} of the dir's committed manifest (its ref), {} if none."""
    sha = ref_get(dir_ref(directory), ws)
    try:
        return {k: v["sha256"] for k, v in read_manifest(sha, ws)["files"].items()} if sha else {}
    except (KeyError, OSError, ValueError, TypeError):
        return {}


def add(directory: PathLike, paths: Optional[list] = None, ws: Optional[PathLike] = None, *,
        exclude_ext: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Stage files (default: all) into the CAS and the staging index; return {rel: sha}."""
    base = Path(directory).resolve()
    want = {Path(x).as_posix().strip("/") for x in paths} if paths else None
    idx = _load_index(ws)
    staged = idx.setdefault(str(base), {})
    for rel, p in _files(base, exclude_ext):
        if want is None or rel in want or any(rel.startswith(w + "/") for w in want):
            staged[rel] = put(p, ws)
    _save_index(ws, idx)
    return dict(staged)


def commit(directory: PathLike, ws: Optional[PathLike] = None, *, task_id: Optional[str] = None,
           inputs: Optional[list] = None, created_at: Optional[str] = None) -> str:
    """Commit the staged set: write ARTIFACT_MANIFEST.json (+ provenance, parent = previous HEAD)
    and manifest blob, move the dir's ref (and ``tasks/<task_id>``); clear the index."""
    base = Path(directory).resolve()
    idx = _load_index(ws)
    staged = {**last_manifest(base, ws), **idx.get(str(base), {})}
    entries = {rel: {"sha256": sha, "size": get_path(sha, ws).stat().st_size} for rel, sha in staged.items()}
    ref = dir_ref(base)
    prov = build_provenance(task_id=task_id, inputs=inputs, parent=ref_get(ref, ws), created_at=created_at)
    sha = write_manifest(base, entries, ws, provenance=prov, ref=ref)
    if prov["task_id"]:
        ref_set(f"tasks/{prov['task_id']}", sha, ws)
    idx.pop(str(base), None)
    _save_index(ws, idx)
    return sha


def diff(directory: PathLike, ws: Optional[PathLike] = None, *,
         exclude_ext: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Optional[str]]]:
    """Working dir vs last manifest by sha: {rel: {"change": added|modified|deleted, old, new}}."""
    base = Path(directory)
    old, out = last_manifest(base, ws), {}
    cur = {rel: _hash_file(p) for rel, p in _files(base, exclude_ext)}
    for rel in sorted(set(old) | set(cur)):
        o, n = old.get(rel), cur.get(rel)
        if o != n:
            out[rel] = {"change": "added" if o is None else "deleted" if n is None else "modified",
                        "old": o, "new": n}
    return out


def status(directory: PathLike, ws: Optional[PathLike] = None, *,
           exclude_ext: Optional[Iterable[str]] = None) -> Dict[str, object]:
    base = Path(directory).resolve()
    staged = _load_index(ws).get(str(base), {})
    changes = diff(base, ws, exclude_ext=exclude_ext)
    return {"staged": sorted(staged),
            "changes": {r: c["change"] for r, c in changes.items()},
            "unstaged": sorted(r for r, c in changes.items()
                               if c["change"] != "deleted" and staged.get(r) != c["new"])}
