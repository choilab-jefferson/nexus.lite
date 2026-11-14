"""Notebook hygiene: README badge coverage, generator determinism, v0.2.2 notebook runs."""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NB_DIR = ROOT / "notebooks"
COLAB = "https://colab.research.google.com/github/choilab-jefferson/nexus.lite/blob/main/notebooks/"


def test_every_notebook_has_readme_badge():
    readme = (ROOT / "README.md").read_text()
    names = [p.name for p in NB_DIR.glob("*.ipynb")]
    assert names
    missing = [n for n in names if f"[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)]({COLAB}{n})" not in readme]
    assert not missing


def test_generator_is_deterministic_and_matches_committed():
    before = {p.name: p.read_text() for p in NB_DIR.glob("*.ipynb")}
    subprocess.run([sys.executable, str(ROOT / "examples" / "make_notebooks.py")], check=True, capture_output=True)
    try:
        for name, text in before.items():
            now = (NB_DIR / name).read_text()
            json.loads(now)
            assert now == text, f"{name} out of date: run examples/make_notebooks.py"
    finally:
        for name, text in before.items():
            (NB_DIR / name).write_text(text)


def test_v022_notebook_code_cells_run(tmp_path):
    nb = json.loads((NB_DIR / "v022_primitives_colab.ipynb").read_text())
    src = "\n\n".join(
        "".join(l for l in c["source"] if not l.lstrip().startswith("!"))
        for c in nb["cells"] if c["cell_type"] == "code"
    )
    script = tmp_path / "nb.py"
    script.write_text(src)
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120, cwd=tmp_path,
                       env={**__import__("os").environ, "PYTHONPATH": str(ROOT), "NEXUS_LITE_HOME": str(tmp_path / "h")})
    assert r.returncode == 0, r.stderr[-2000:]
    assert "run_on_colab result" in r.stdout and "BLOCKED" in r.stdout
