"""Optional MLflow tracking adapter: a VIEW over the hash manifest.

The hash manifest (sha256 over artifacts) is authoritative; MLflow only mirrors it via
tags ``nexus.manifest_sha`` / ``nexus.task_id`` / ``nexus.commit``. ``mlflow`` is an
optional extra (``pip install nexus-lite[mlflow]``), imported lazily inside functions,
and this module is never shipped in the kernel bundle (see ``compute.py``).

The default tracking store is a sqlite file under ``<ws>/.nexus/mlflow.db``, separate
from lite's WAL DB and refused on Drive FUSE mounts (sqlite locking is unsafe there).
``MLFLOW_TRACKING_URI`` overrides it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

INSTALL_HINT = "mlflow is not installed: pip install nexus-lite[mlflow]"
_DRIVE_PREFIXES = ("/content/drive", "/mnt/drive", "/Volumes/GoogleDrive")
_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Obvious PHI shapes: SSN, e-mail, MRN-labelled numbers, ISO/US dates (DOB-like).
_PHI_RES = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),
    re.compile(r"\b(?:mrn|ssn|dob|patient)\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
)


def _import_mlflow() -> Any:
    try:
        import mlflow  # noqa: PLC0415 - lazy by design
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    return mlflow


def _on_drive(path: Path) -> bool:
    s = os.path.realpath(path)
    return any(s == p or s.startswith(p + "/") for p in _DRIVE_PREFIXES)


def default_tracking_uri(workspace: Optional[os.PathLike] = None) -> str:
    """``MLFLOW_TRACKING_URI`` if set, else ``sqlite:///<ws>/.nexus/mlflow.db``."""
    env = os.environ.get("MLFLOW_TRACKING_URI")
    if env:
        return env
    db = Path(workspace or Path.cwd()).resolve() / ".nexus" / "mlflow.db"
    if _on_drive(db):
        raise ValueError(
            f"refusing mlflow sqlite DB on a Drive FUSE mount ({db}); use a local "
            "workspace or set MLFLOW_TRACKING_URI"
        )
    db.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db}"


def _is_local_uri(uri: str) -> bool:
    return uri.startswith(("sqlite:", "file:")) or "://" not in uri


def _check_phi_free(kind: str, key: str, value: Any) -> None:
    text = f"{key}={value}"
    if any(r.search(text) for r in _PHI_RES):
        raise ValueError(f"PHI gate: {kind} {key!r} looks like PHI; refused")


def track(
    manifest_sha: str,
    provenance: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    metrics: Optional[Mapping[str, float]] = None,
    *,
    workspace: Optional[os.PathLike] = None,
) -> str:
    """Log one MLflow run mirroring a manifest; return the run id.

    ``provenance`` is the manifest's provenance block (``cas.read_manifest(sha)["provenance"]``);
    ``task_id`` and ``commit`` tags come from it (``commit`` omitted when unknown).
    """
    task_id, commit = provenance.get("task_id"), provenance.get("commit")
    if not _HEX_RE.match(manifest_sha or ""):
        raise ValueError("manifest_sha must be a lowercase hex digest")
    if not _ID_RE.match(task_id or "") or (commit is not None and not _ID_RE.match(commit)):
        raise ValueError("task_id/commit must be short [A-Za-z0-9._:-] identifiers")
    params, metrics = dict(params or {}), dict(metrics or {})
    for k, v in params.items():
        _check_phi_free("param", k, v)
    for k in metrics:
        _check_phi_free("metric", k, "")
    mlflow = _import_mlflow()
    uri = default_tracking_uri(workspace)
    if os.environ.get("NEXUS_SENSITIVITY") == "phi" and not _is_local_uri(uri):
        raise ValueError("PHI gate: remote MLflow tracking URI refused under NEXUS_SENSITIVITY=phi")
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("nexus-lite")
    tags = {"nexus.manifest_sha": manifest_sha, "nexus.task_id": task_id}
    if commit:
        tags["nexus.commit"] = commit
    with mlflow.start_run(run_name=task_id, tags=tags) as run:
        for k, v in params.items():
            mlflow.log_param(k, v)
        for k, v in metrics.items():
            mlflow.log_metric(k, float(v))
        return run.info.run_id
