"""Kernel/demo split contract: the core never imports demos/colab."""

from __future__ import annotations

import subprocess
import sys
import warnings
from importlib import metadata
from pathlib import Path

import pytest

import nexus_lite

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_DIR = REPO_ROOT / "nexus_lite"
BANNED = ("nexus_lite.demos", "nexus_lite.colab", "nexus_lite.viz", "nexus_lite.loom_")


def _py(code: str, *flags: str):
    return subprocess.run([sys.executable, *flags, "-c", code], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)


def test_core_and_cli_do_not_import_demos_or_colab():
    res = _py("import sys, nexus_lite.core, nexus_lite.cli; "
              f"bad=[m for m in sys.modules if m.startswith({BANNED!r})]; assert not bad, bad")
    assert res.returncode == 0, res.stderr


def test_kernel_sources_have_no_demo_or_colab_imports():
    """Static check: only the lazy deprecation shims may name the moved packages."""
    allowed = {"__init__.py", "core.py"}  # lazy __getattr__ shims
    for path in KERNEL_DIR.glob("*.py"):
        if path.name in allowed or path.name in ("viz.py", "loom_multi_agent.py",
                                                  "loom_multi_task.py", "loom_radiomics_tasks.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("from . import demos", "from .demos", "from .colab", "import nexus_lite.demos",
                       "import nexus_lite.colab", "from . import colab"):
            assert needle not in text, f"{path.name}: {needle}"


def test_lite_itself_clean_under_error_deprecation():
    res = _py("import nexus_lite, nexus_lite.core, nexus_lite.cli", "-W", "error::DeprecationWarning")
    assert res.returncode == 0, res.stderr


def test_get_db_not_public():
    assert "get_db" not in nexus_lite.__all__ and "get_db_path" not in nexus_lite.__all__
    from nexus_lite._db import get_db, get_db_path  # still importable from the private home

    assert callable(get_db) and callable(get_db_path)


def test_version_single_source():
    assert metadata.version("nexus-lite") == nexus_lite.__version__


def test_deprecated_names_still_resolve_with_warning():
    from nexus_lite import core

    with pytest.warns(DeprecationWarning, match="nexus_lite.demos.orchestration"):
        fn = core.run_multi_tier_swarm
    from nexus_lite.demos import orchestration

    assert fn is orchestration.run_multi_tier_swarm
    with pytest.warns(DeprecationWarning, match="colab.env"):
        assert core.detect_env is __import__("nexus_lite.colab.env", fromlist=["x"]).detect_env
    with pytest.warns(DeprecationWarning):
        assert nexus_lite.run_phantom_shape_swarm is __import__(
            "nexus_lite.demos.scenarios", fromlist=["x"]).run_phantom_shape_swarm
    with pytest.raises(AttributeError):
        core.definitely_not_a_name


def test_old_module_paths_are_deprecated_shims():
    res = _py("import warnings; warnings.simplefilter('error', DeprecationWarning)\n"
              "try:\n    import nexus_lite.viz\nexcept DeprecationWarning:\n    print('warned')\n")
    assert res.stdout.strip() == "warned", res.stderr
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import nexus_lite.viz as old
    assert old is sys.modules["nexus_lite.demos.viz"]


def test_cli_surfaces_split():
    moved = ("demo", "multi-tier", "interact", "cooperate", "dashboard")
    kernel = _py("from nexus_lite import cli; cli.main(['--help'])")
    demo = _py("from nexus_lite.demos import cli; cli.main(['--help'])")
    for name in moved:
        assert f" {name} " not in kernel.stdout.replace("\n", " ") and f"\n    {name}" not in kernel.stdout, name
        assert name in demo.stdout, name
    for name in ("init", "pty", "bus", "delegate", "wiki", "stm", "compute"):
        assert name in kernel.stdout


def test_bundle_excludes_demos_unless_extras():
    import io
    import zipfile

    from nexus_lite import compute

    def names(extras):
        raw, _ = compute.pack_lite_bundle(include_extras=extras)
        return zipfile.ZipFile(io.BytesIO(raw)).namelist()

    kernel = names(False)
    assert not [n for n in kernel if n.startswith("nexus_lite/demos/")]
    assert "nexus_lite/colab/env.py" in kernel and "nexus_lite/core.py" in kernel
    assert any(n.startswith("nexus_lite/demos/") for n in names(True))


def test_bundle_version_matches_package(tmp_path):
    import io
    import subprocess
    import sys
    import zipfile

    import nexus_lite
    from nexus_lite import compute

    raw, _ = compute.pack_lite_bundle(include_extras=False)
    zipfile.ZipFile(io.BytesIO(raw)).extractall(tmp_path)
    out = subprocess.run(
        # -S: no site-packages, so neither an installed nexus_lite nor its metadata can answer
        [sys.executable, "-S", "-c",
         "import nexus_lite; print(nexus_lite.__file__); print(nexus_lite.__version__)"],
        cwd=tmp_path, env={"PYTHONPATH": str(tmp_path)}, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert out[0].startswith(str(tmp_path))
    assert out[1] == nexus_lite.__version__ != "0+unknown"
