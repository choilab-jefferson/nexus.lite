"""Compute backend plugin hook: entry point group + optional `nexus_lite_private` overlay."""
import json
import subprocess
import sys
import types

import pytest

from nexus_lite import cli, compute


@pytest.fixture(autouse=True)
def _fresh_backend_cache():
    compute._load_backends.cache_clear()
    yield
    compute._load_backends.cache_clear()


class _Fake:
    def run(self, **kw):
        return {"ok": True, "target": "fake", "kw": sorted(kw)}

    def add_cli_arguments(self, parser):
        parser.add_argument("--fake-flag", default=None)

    def cli_run(self, args, **common):
        return {"ok": True, "flag": args.fake_flag, "python": common["python"]}


def _no_overlay(monkeypatch):
    monkeypatch.setitem(sys.modules, "nexus_lite_private", None)  # import raises ModuleNotFoundError


def _entry_points(monkeypatch, eps):
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group=None: eps if group == compute.BACKEND_GROUP else [])


class _EP:
    def __init__(self, name, obj):
        self.name, self._obj = name, obj

    def load(self):
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def test_public_targets_without_overlay(monkeypatch):
    _no_overlay(monkeypatch)
    _entry_points(monkeypatch, [])
    assert compute.available_targets() == ["colab", "local"]
    st = compute.overlay_status()
    assert st["installed"] is False and st["errors"] == []
    with pytest.raises(ValueError, match="'colab', 'local'"):
        compute.compute_run("nowhere")


def test_entry_point_backend_runs_and_cannot_shadow_public(monkeypatch):
    _no_overlay(monkeypatch)
    _entry_points(monkeypatch, [_EP("fake", _Fake()), _EP("local", _Fake()), _EP("bad", RuntimeError("boom"))])
    assert compute.available_targets() == ["colab", "local", "fake"]
    res = compute.compute_run("fake", python="RESULT = 1")
    assert res["target"] == "fake" and "python" in res["kw"]
    assert compute.compute_run("local", python="RESULT = 2")["result"] == 2  # public local, not the plugin
    st = compute.overlay_status()
    assert st["entry_point_targets"] == ["fake"] and any("bad" in e for e in st["errors"])


def test_callable_backend_and_import_fallback(monkeypatch):
    _entry_points(monkeypatch, [])
    mod = types.ModuleType("nexus_lite_private")
    mod.__version__ = "9.9"
    mod.COMPUTE_BACKENDS = {"Fn": lambda **kw: {"ok": True, "kw": sorted(kw)}}
    monkeypatch.setitem(sys.modules, "nexus_lite_private", mod)
    st = compute.overlay_status()
    assert st["installed"] is True and st["version"] == "9.9" and st["extra_targets"] == ["fn"]
    assert compute.compute_run("fn", cmd="true")["ok"] is True


def test_broken_overlay_is_reported_not_raised(monkeypatch):
    _entry_points(monkeypatch, [])

    class _Boom(types.ModuleType):
        def __getattr__(self, name):
            raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "nexus_lite_private", _Boom("nexus_lite_private"))
    st = compute.overlay_status()
    assert st["installed"] is True and st["extra_targets"] == [] and any("boom" in e for e in st["errors"])


def test_cli_compute_plugin_target_and_info(monkeypatch, capsys):
    _no_overlay(monkeypatch)
    _entry_points(monkeypatch, [_EP("fake", _Fake())])
    assert cli.main(["compute", "fake", "--fake-flag", "x", "--python", "RESULT = 1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "flag": "x", "python": "RESULT = 1"}
    assert cli.main(["info"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["compute_targets"] == ["colab", "local", "fake"]
    assert info["private_overlay"]["installed"] is False and info["version"]


def test_env_fields_land_in_result(monkeypatch):
    monkeypatch.setenv("NXL_TEST_A", "")
    monkeypatch.setenv("NXL_TEST_B", "bee")
    src, _sha, _jid = compute.pack_self_contained_script(
        python="RESULT = 1", env_fields={"x": ["NXL_TEST_A", "NXL_TEST_B"], "y": ["NXL_TEST_UNSET"]})
    import os
    env = dict(os.environ)
    env["NEXUS_LITE_RUNTIME_ROOT"] = os.path.join(os.environ.get("TMPDIR", "/tmp"), "nxl-envfields")
    proc = subprocess.run([sys.executable, "-I", "-"], input=src, text=True, capture_output=True, env=env, timeout=120)
    res = compute.parse_runner_output(proc.stdout, proc.stderr, proc.returncode)
    assert res["ok"] is True and res["x"] == "bee" and res["y"] is None
