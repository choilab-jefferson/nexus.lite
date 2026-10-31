"""Release snapshot export + leak check. Hermetic: temp git repo, no network."""
import importlib.util
import json
import subprocess
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "publish_snapshot", Path(__file__).resolve().parent.parent / "scripts" / "publish_snapshot.py")
ps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ps)

# Leak-triggering literals are assembled at runtime so this file itself is leak-clean.
HOST = "ser49" + "88-a.tjh." + "tju.edu"
WT = ".work" + "trees"
HOME = "/ho" + "me/wxc" + "151/x\n"

CFG = {"allowlist": ["nexus_lite/", "notebooks/", "README.md", ".gitignore"],
       "public_author_name": "Pub", "public_author_email": "pub@example.com",
       "license": "MIT", "strip_notebook_outputs": "if-flagged"}


def _nb(out_text):
    return json.dumps({"cells": [{"cell_type": "code", "source": ["1"], "execution_count": 1,
                                  "outputs": [{"output_type": "stream", "text": [out_text]}]}],
                       "metadata": {}, "nbformat": 4, "nbformat_minor": 5})


def _repo(tmp_path, extra=None):
    r = tmp_path / "repo"
    files = {
        "nexus_lite/a.py": "x = 1\n", "README.md": "hi\n", ".gitignore": WT + "/\n",
        ".gitlab-ci.yml": "# " + HOST + "\n", "AGENTS.md": "agents\n",
        WT + "/w/f.txt": "wt\n", "notebooks/leaky.ipynb": _nb(HOME),
        "notebooks/clean.ipynb": _nb("ok\n"),
    }
    files.update(extra or {})
    for k, v in files.items():
        p = r / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v)
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "-A", "-f")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return r


def test_allowlist_excludes_internal(tmp_path):
    r = _repo(tmp_path)
    d = tmp_path / "out"
    ps.export("HEAD", d, CFG, repo=str(r))
    got = {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}
    assert "nexus_lite/a.py" in got and "README.md" in got
    assert ".gitlab-ci.yml" not in got and "AGENTS.md" not in got
    assert not any(g.startswith(WT) for g in got)
    assert ps.leak_check(d) == []


def test_leak_check_fails_on_planted_hostname(tmp_path):
    d = tmp_path / "x"; d.mkdir()
    (d / "f.py").write_text("ok\nurl = '" + HOST + "'\n")
    hits = ps.leak_check(d)
    assert "f.py:2:internal-host" in hits
    assert ps.main(["leak-check", str(d)]) == 1


def test_leak_check_passes_on_clean(tmp_path):
    d = tmp_path / "x"; d.mkdir()
    (d / "f.py").write_text("print('hi')\n")
    assert ps.main(["leak-check", str(d)]) == 0


def test_flagged_notebook_stripped_clean_kept(tmp_path):
    r = _repo(tmp_path)
    d = tmp_path / "out"
    ps.export("HEAD", d, CFG, repo=str(r))
    leaky = json.loads((d / "notebooks/leaky.ipynb").read_text())
    clean = json.loads((d / "notebooks/clean.ipynb").read_text())
    assert leaky["cells"][0]["outputs"] == []
    assert clean["cells"][0]["outputs"] != []


def test_snapshot_single_commit_no_remote(tmp_path):
    r = _repo(tmp_path)
    d = tmp_path / "out"
    full = ps.export("HEAD", d, CFG, repo=str(r))
    ps.snapshot(d, "v1.0.0", full, CFG)
    log = subprocess.check_output(["git", "-C", str(d), "log", "--format=%s|%an"], text=True).splitlines()
    assert len(log) == 1 and log[0].startswith("Release v1.0.0 (snapshot of nexus.lite@") and log[0].endswith("|Pub")
    assert subprocess.check_output(["git", "-C", str(d), "remote"], text=True).strip() == ""
