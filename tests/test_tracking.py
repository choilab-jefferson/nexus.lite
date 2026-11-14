"""Optional MLflow tracking adapter."""
from __future__ import annotations

import builtins
import importlib
import sys
import zipfile
import io

import pytest

from nexus_lite import compute, tracking

PROV = {"task_id": "t1", "commit": "deadbeef"}
SHA = "ab" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("NEXUS_SENSITIVITY", raising=False)
    monkeypatch.chdir(tmp_path)


def _block_mlflow(monkeypatch):
    monkeypatch.delitem(sys.modules, "mlflow", raising=False)
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "mlflow" or name.startswith("mlflow."):
            raise ImportError("No module named 'mlflow'")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_import_and_call_without_mlflow(monkeypatch):
    _block_mlflow(monkeypatch)
    importlib.reload(tracking)  # module import must not need mlflow
    with pytest.raises(RuntimeError, match=r"pip install nexus-lite\[mlflow\]"):
        tracking.track(SHA, PROV)


def test_default_uri_under_workspace_and_env_override(tmp_path, monkeypatch):
    uri = tracking.default_tracking_uri(tmp_path)
    assert uri == f"sqlite:///{tmp_path.resolve()}/.nexus/mlflow.db"
    assert not uri.endswith("lite.db")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow.example:5000")
    assert tracking.default_tracking_uri(tmp_path) == "http://mlflow.example:5000"


def test_refuses_drive_fuse(monkeypatch):
    monkeypatch.setattr(tracking, "_on_drive", lambda p: True)
    with pytest.raises(ValueError, match="Drive"):
        tracking.default_tracking_uri("/content/drive/MyDrive/ws")


@pytest.mark.parametrize("bad", [{"patient": "x"}, {"note": "john@x.org"}, {"d": "1980-01-02"}, {"s": "123-45-6789"}])
def test_rejects_phi_params(bad):
    with pytest.raises(ValueError, match="PHI"):
        tracking.track(SHA, PROV, params=bad)


def test_rejects_bad_identifiers():
    with pytest.raises(ValueError):
        tracking.track("not-hex!", PROV)
    with pytest.raises(ValueError):
        tracking.track(SHA, {**PROV, "task_id": "John Doe 1980-01-02"})


def test_phi_mode_refuses_remote_uri(monkeypatch):
    pytest.importorskip("mlflow")
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "https://mlflow.example")
    with pytest.raises(ValueError, match="PHI gate"):
        tracking.track(SHA, PROV)


def test_track_with_mlflow_sets_tags(tmp_path):
    mlflow = pytest.importorskip("mlflow")
    run_id = tracking.track(SHA, PROV, params={"lr": 0.1}, metrics={"auc": 0.9}, workspace=tmp_path)
    assert (tmp_path / ".nexus" / "mlflow.db").exists()
    run = mlflow.get_run(run_id)
    assert run.data.tags["nexus.manifest_sha"] == SHA
    assert run.data.tags["nexus.task_id"] == "t1"
    assert run.data.tags["nexus.commit"] == "deadbeef"
    assert run.data.params["lr"] == "0.1" and run.data.metrics["auc"] == 0.9


def test_kernel_bundle_excludes_tracking_and_mlflow():
    raw, _ = compute.pack_lite_bundle(include_extras=False)
    zf = zipfile.ZipFile(io.BytesIO(raw))
    assert "nexus_lite/tracking.py" not in zf.namelist()
    for name in zf.namelist():
        if name.endswith(".py"):
            src = zf.read(name).decode()
            assert "import mlflow" not in src and "from mlflow" not in src, name
