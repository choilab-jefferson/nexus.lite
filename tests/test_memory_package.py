"""nexus_lite.memory.{stm,mtm,ltm}: import isolation, deprecation shims, name identity (lite#20)."""

import subprocess
import sys
import textwrap
import warnings

import pytest

TIERS = ("stm", "mtm", "ltm")
OTHER = {t: [f"nexus_lite.memory.{o}" for o in TIERS if o != t] for t in TIERS}
FORBIDDEN = ["nexus_lite.core", "nexus_lite.colab", "nexus_lite.demos", "nexus_lite.viz"]


def _run(code: str, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *flags, "-c", textwrap.dedent(code)], capture_output=True, text=True
    )


@pytest.mark.parametrize("tier", TIERS)
def test_tier_import_isolation(tier):
    bad = OTHER[tier] + FORBIDDEN + [m for m in ("nexus_lite.loom",)]
    res = _run(
        f"""
        import sys, nexus_lite.memory.{tier}
        hits = [m for m in {bad!r} if m in sys.modules]
        hits += [m for m in sys.modules if m.startswith('nexus_lite.loom')]
        print(hits)
        """
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "[]"


def test_memory_package_imports_no_tier():
    res = _run(
        """
        import sys, nexus_lite.memory
        print([m for m in sys.modules if m.startswith('nexus_lite.memory.')])
        """
    )
    assert res.stdout.strip() == "[]"


def test_lite_itself_is_deprecation_clean():
    res = _run(
        """
        import nexus_lite, nexus_lite.memory, nexus_lite.core, nexus_lite.cli
        import nexus_lite.colab.tunnel
        nexus_lite.bus_send, nexus_lite.wiki_write, nexus_lite.stm_write
        """,
        "-W",
        "error::DeprecationWarning",
    )
    assert res.returncode == 0, res.stderr


def test_old_stm_module_path_warns():
    res = _run("import nexus_lite.stm", "-W", "error::DeprecationWarning")
    assert res.returncode != 0 and "DeprecationWarning" in res.stderr
    res = _run(
        """
        import warnings
        warnings.simplefilter('ignore')
        import nexus_lite.stm as old
        import nexus_lite.memory.stm as new
        assert old is new
        """
    )
    assert res.returncode == 0, res.stderr


MOVED = {
    "mtm": ["bus_send", "bus_read"],
    "ltm": ["wiki_write", "wiki_read", "wiki_search", "wiki_list", "_wiki_path"],
    "stm": ["stm_write", "stm_read", "stm_list", "stm_rebuild_index", "stm_search", "stm_pack"],
}


@pytest.mark.parametrize("tier,name", [(t, n) for t, ns in MOVED.items() for n in ns])
def test_core_old_names_warn_and_are_identical(tier, name):
    import importlib

    from nexus_lite import core

    new = getattr(importlib.import_module(f"nexus_lite.memory.{tier}"), name)
    with pytest.warns(DeprecationWarning, match=name):
        old = getattr(core, name)
    assert old is new


@pytest.mark.parametrize("tier,name", [(t, n) for t, ns in MOVED.items() for n in ns if n[0] != "_"])
def test_top_level_names_unchanged_and_silent(tier, name):
    import importlib

    import nexus_lite as nl

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert getattr(nl, name) is getattr(importlib.import_module(f"nexus_lite.memory.{tier}"), name)


def test_old_core_names_still_work(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_DIR", str(tmp_path))
    from nexus_lite import core

    with pytest.warns(DeprecationWarning):
        assert core.bus_send("x", "hello")["target"] == "x"
        core.wiki_write("a/b", "# hi")
        core.stm_write(kind="task", summary="s")
    assert core.get_workspace() == tmp_path.resolve()
