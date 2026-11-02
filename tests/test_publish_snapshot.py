"""Release snapshot export + leak check. Hermetic: temp git repo, no network."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "publish_snapshot.py"
if not _SCRIPT.exists():   # scripts/ is not in the public snapshot
    pytest.skip("scripts/publish_snapshot.py not exported", allow_module_level=True)

SPEC = importlib.util.spec_from_file_location(
    "publish_snapshot", Path(__file__).resolve().parent.parent / "scripts" / "publish_snapshot.py")
ps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ps)

# Leak-triggering literals are assembled at runtime so this file itself is leak-clean.
HOST = "ser49" + "88-a.tjh." + "tju." + "edu"
WT = ".work" + "trees"
HOME = "/ho" + "me/wxc" + "151/x\n"

CFG = {"allowlist": ["nexus_lite/", "notebooks/", "README.md", ".gitignore"],
       "public_author_name": "Pub", "public_author_email": "pub@example.com",
       "license": "MIT", "strip_notebook_outputs": "if-flagged",
       "dist_name": "nexus-lite", "source_name": "nexus.lite", "version_source": "none"}


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


# ---- release ----
def _rel(tmp_path, cfg=None, extra=None, **kw):
    r = _repo(tmp_path, extra)
    c = dict(CFG, github_url="git@example.com:o/r.git", **(cfg or {}))
    out = tmp_path / "rel"
    return ps.release(kw.pop("version", "v1.2.3"), "HEAD", out, c, repo=str(r), **kw), out


def test_release_dry_run_prints_push_cmds(tmp_path, capsys):
    rc, out = _rel(tmp_path, cfg={"license": "TODO x"}, dry_run=True)
    o = capsys.readouterr()
    assert rc == 0 and not out.exists()
    assert "leak-check: OK" in o.out and "WARNING" in o.err
    assert "--force" not in o.out
    assert "git fetch git@example.com:o/r.git main:gh-main" in o.out
    assert "git read-tree -u --reset release/v1.2.3" in o.out
    assert "git push git@example.com:o/r.git publish/v1.2.3:refs/heads/main" in o.out
    assert "git push git@example.com:o/r.git refs/tags/v1.2.3" in o.out


def test_release_todo_config_refuses(tmp_path, capsys):
    rc, out = _rel(tmp_path, cfg={"public_author_name": "TODO owner"})
    assert rc == 2 and not out.exists()
    assert "public_author_name" in capsys.readouterr().err


def test_release_real_makes_bundle(tmp_path):
    rc, out = _rel(tmp_path, extra={"LICENSE": "MIT\n"}, cfg={
        "allowlist": CFG["allowlist"] + ["LICENSE"]})
    assert rc == 0
    b = out / "nexus-lite-v1.2.3.bundle"
    heads = subprocess.check_output(["git", "bundle", "list-heads", str(b)], text=True)
    assert "refs/heads/release/v1.2.3" in heads and "refs/tags/v1.2.3" in heads
    tree = out / "tree"
    n = subprocess.check_output(["git", "-C", str(tree), "rev-list", "--count", "HEAD"], text=True)
    assert n.strip() == "1"
    for nb in ("leaky", "clean"):
        assert json.loads((tree / ("notebooks/%s.ipynb" % nb)).read_text())["cells"][0]["outputs"] == []


def test_release_real_missing_license_refuses(tmp_path):
    rc, out = _rel(tmp_path)
    assert rc == 2 and not (out / "nexus-lite-v1.2.3.bundle").exists()


def test_release_bad_version(tmp_path):
    rc, out = _rel(tmp_path, version="1.2")
    assert rc == 2 and not out.exists()


def test_release_leak_hit_rc1(tmp_path):
    rc, out = _rel(tmp_path, extra={"nexus_lite/b.py": "u='" + HOST + "'\n", "LICENSE": "MIT\n"},
                   cfg={"allowlist": CFG["allowlist"] + ["LICENSE"]})
    assert rc == 1 and not (out / "nexus-lite-v1.2.3.bundle").exists()


# ---- repo-agnostic behaviour (fake non-lite config) ----
FAKE = {"allowlist": ["pkg/", "pyproject.toml", "LICENSE"],
        "public_author_name": "Pub Lisher", "public_author_email": "pub@example.org",
        "license": "MIT", "dist_name": "fakeproj", "source_name": "fake.src",
        "bundle_name": "fakebundle", "tag_prefix": "rel-", "version_source": "pyproject",
        "extra_leak_patterns": [["fake-secret", "FAKE_SECRET_[A-Z]+"]],
        "github_url": "git@example.com:o/fake.git"}
FAKE_FILES = {"pkg/a.py": "x = 1\n", "LICENSE": "MIT\n",
              "pyproject.toml": '[build-system]\nrequires=[]\n[project]\nname="fakeproj"\nversion = "3.4.5"\n'}


def test_fake_config_dry_run_ok(tmp_path, capsys):
    r = _repo(tmp_path, FAKE_FILES)
    rc = ps.release("rel-3.4.5", "HEAD", tmp_path / "o", FAKE, dry_run=True, repo=str(r))
    o = capsys.readouterr().out
    assert rc == 0 and "leak-check: OK" in o and "fakebundle-rel-3.4.5.bundle" in o
    assert '--author="Pub Lisher <pub@example.org>"' in o
    assert "nexus-lite" not in o


def test_fake_config_extra_pattern_and_tag_prefix(tmp_path):
    r = _repo(tmp_path, dict(FAKE_FILES, **{"pkg/b.py": "k='FAKE_SECRET_ABC'\n"}))
    assert ps.release("rel-3.4.5", "HEAD", tmp_path / "o", FAKE, dry_run=True, repo=str(r)) == 1
    assert ps.release("v3.4.5", "HEAD", tmp_path / "o2", FAKE, dry_run=True, repo=str(r)) == 2


def test_version_mismatch_refuses_real_release(tmp_path, capsys):
    r = _repo(tmp_path, FAKE_FILES)
    rc = ps.release("rel-9.9.9", "HEAD", tmp_path / "o", FAKE, repo=str(r))
    assert rc == 2 and "pyproject version" in capsys.readouterr().err


def test_identity_check_rejects_foreign_commit(tmp_path):
    d = tmp_path / "t"; d.mkdir()
    (d / "f").write_text("ok\n")
    g = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "-A")
    g("-c", "user.name=Nexus Bot", "-c", "user.email=bot@users.noreply.example", "commit", "-qm", "c")
    hits = ps.leak_check(d, FAKE)
    assert any(h.endswith(":author-identity") for h in hits)
    assert any(h.endswith(":committer-identity") for h in hits)
    assert ps.leak_check(d) == []   # no config -> no identity check


def test_snapshot_forces_identity_over_local_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone"); monkeypatch.setenv("GIT_COMMITTER_EMAIL", "x@y.z")
    r = _repo(tmp_path, FAKE_FILES)
    d = tmp_path / "out"
    full = ps.export("HEAD", d, FAKE, repo=str(r))
    ps.snapshot(d, "rel-3.4.5", full, FAKE)
    assert ps.leak_check(d, FAKE) == []


def test_script_info(capsys):
    assert ps.main(["script-info"]) == 0
    assert "SCRIPT_VERSION=" in capsys.readouterr().out
