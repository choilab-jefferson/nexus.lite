import hashlib
import json
import os
import sqlite3
import stat

import pytest

from nexus_lite import _db, cas


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    _db.reset_init()
    _db.init(w)
    return w


def _rows():
    with _db.connect() as c:
        return c.execute("SELECT sha256, size, first_task_id FROM cas_objects").fetchall()


def test_put_bytes_get_has_layout_and_mode(ws):
    sha = cas.put(b"hello")
    assert sha == hashlib.sha256(b"hello").hexdigest()
    p = ws / "cas" / "sha256" / sha[:2] / sha[2:]
    assert cas.has(sha) and cas.get(sha) == b"hello" and cas.get_path(sha) == p
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not cas.has("0" * 64)
    with pytest.raises(KeyError):
        cas.get("0" * 64)
    with pytest.raises(ValueError):
        cas.has("../etc")


def test_put_path_and_task_id(ws, tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc" * 1000)
    sha = cas.put(f, task_id="t1")
    assert cas.get(sha) == b"abc" * 1000
    assert [tuple(r) for r in _rows()] == [(sha, 3000, "t1")]


def test_dedupe_no_rewrite_no_duplicate_row(ws):
    sha = cas.put(b"same", task_id="t1")
    p = cas.get_path(sha)
    os.utime(p, (1, 1))
    cas.put(b"same", task_id="t2")
    assert p.stat().st_mtime == 1  # blob untouched
    rows = _rows()
    assert len(rows) == 1 and rows[0]["first_task_id"] == "t1"
    assert [x.name for x in p.parent.iterdir()] == [p.name]  # no temp leftovers


def test_manifest(ws, tmp_path):
    d = tmp_path / "out"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("A")
    (d / "sub" / "b.txt").write_text("B")
    (d / "link").symlink_to(d / "a.txt")
    m, msha = cas.manifest(d)
    assert set(m) == {"a.txt", "sub/b.txt"}
    assert m["a.txt"] == {"sha256": hashlib.sha256(b"A").hexdigest(), "size": 1}
    assert json.loads(cas.get(msha)) == {"schema": 1, "files": m}
    assert cas.manifest(d)[1] == msha  # deterministic
    assert len(_rows()) == 3


def test_other_workspace_is_blob_only(ws, tmp_path):
    other = tmp_path / "other"
    sha = cas.put(b"z", other)
    assert cas.has(sha, other) and not cas.has(sha)
    assert _rows() == []


def test_migration_creates_table_on_v3_db(ws):
    db = ws / "lite.db"
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE cas_objects")
    con.execute("PRAGMA user_version = 3")
    con.commit()
    con.close()
    _db.reset_init()
    _db.init(ws, force=True)
    assert _db.SCHEMA_VERSION >= 4
    assert _rows() == []


# ---- slice 2: manifest file, offline verbs ----
def test_manifest_file_and_excluded_from_own_hash(ws, tmp_path):
    d = tmp_path / "t"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_bytes(b"A")
    (d / "sub" / "b.txt").write_bytes(b"B")
    entries, msha = cas.manifest(d)
    assert cas.write_manifest(d, entries, task_id="t1") == msha
    mf = d / "ARTIFACT_MANIFEST.json"
    assert stat.S_IMODE(mf.stat().st_mode) == 0o600
    assert json.loads(mf.read_text()) == {"schema": 1, "files": entries} and cas.get(msha) == mf.read_bytes()
    assert cas.manifest(d)[1] == msha  # manifest file does not feed its own hash


def test_verbs_add_commit_status_diff(ws, tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "a").write_bytes(b"1")
    (d / "b").write_bytes(b"2")
    st = cas.status(d)
    assert st["staged"] == [] and st["changes"] == {"a": "added", "b": "added"}
    cas.add(d, ["a"])
    st = cas.status(d)
    assert st["staged"] == ["a"] and st["unstaged"] == ["b"]
    assert cas.has(hashlib.sha256(b"1").hexdigest()) and not cas.has(hashlib.sha256(b"2").hexdigest())
    cas.commit(d)
    assert list(cas.last_manifest(d)) == ["a"] and cas.status(d)["staged"] == []
    (d / "a").write_bytes(b"1x")
    (d / "b").unlink()
    (d / "c").write_bytes(b"3")
    cas.add(d, ["b"])
    ch = cas.diff(d)
    assert {r: c["change"] for r, c in ch.items()} == {"a": "modified", "c": "added"}
    assert ch["a"]["old"] == hashlib.sha256(b"1").hexdigest() and ch["a"]["new"] == hashlib.sha256(b"1x").hexdigest()
    assert "b" not in ch  # never committed, now gone: nothing to report


def test_cli_artifacts_verbs(ws, tmp_path, capsys):
    from nexus_lite import cli
    d = tmp_path / "w"
    d.mkdir()
    (d / "x").write_bytes(b"x")
    assert cli.main(["artifacts", "add", "--dir", str(d)]) == 0
    assert cli.main(["artifacts", "commit", "--dir", str(d)]) == 0
    capsys.readouterr()
    (d / "x").write_bytes(b"y")
    assert cli.main(["artifacts", "diff", "--dir", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["x"]["change"] == "modified"

def test_put_on_uninitialized_workspace(tmp_path, monkeypatch):
    w = tmp_path / "fresh"
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    _db.reset_init()
    sha = cas.put(b"hello", task_id="t")  # no _db.init() beforehand
    assert [r["sha256"] for r in _rows()] == [sha]


def test_default_workspace_has_single_nexus_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_LITE_HOME", raising=False)
    monkeypatch.delenv("NEXUS_LITE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    _db.reset_init()
    sha = cas.put(b"d")
    assert (tmp_path / ".nexus" / "cas" / "sha256" / sha[:2] / sha[2:]).is_file()
    assert not (tmp_path / ".nexus" / ".nexus").exists()
    assert len(_rows()) == 1


def test_put_streams_without_read_bytes(ws, tmp_path, monkeypatch):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * (3 << 20))

    def boom(self):
        raise AssertionError("read_bytes called")
    monkeypatch.setattr(type(f), "read_bytes", boom)
    sha = cas.put(f)
    assert sha == hashlib.sha256(b"x" * (3 << 20)).hexdigest()


def test_cas_dir_chain_private(tmp_path):
    other = tmp_path / "o"
    old = os.umask(0o022)
    try:
        sha = cas.put(b"p", other)
    finally:
        os.umask(old)
    p = cas.get_path(sha, other)
    for d in (p.parent, p.parent.parent, p.parent.parent.parent):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d


def test_manifest_missing_dir_raises(ws, tmp_path):
    with pytest.raises(FileNotFoundError):
        cas.manifest(tmp_path / "nope")
    assert _rows() == []


def test_get_detects_corruption_and_put_repairs(ws):
    sha = cas.put(b"good")
    p = cas.get_path(sha)
    p.write_bytes(b"bad!")
    with pytest.raises(ValueError):
        cas.get(sha)
    assert cas.put(b"good") == sha
    assert cas.get(sha) == b"good"


# ---- refs + provenance  ----
PIN = "2026-11-05T00:00:00Z"
PROD = {"agent": "a", "provider": "p", "model": "m"}


def test_refs_set_get_list_atomic_private(ws, monkeypatch):
    s1, s2 = cas.put(b"1"), cas.put(b"2")
    assert cas.ref_get("tasks/t1") is None
    cas.ref_set("tasks/t1", s1)
    cas.ref_set("tasks/t2", s2)
    cas.ref_set("tasks/t1", s2)  # mutable
    p = ws / "cas" / "refs" / "tasks" / "t1"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and cas.ref_get("tasks/t1") == s2
    assert cas.ref_list("tasks") == {"tasks/t1": s2, "tasks/t2": s2}
    for bad in ("x", "../a/b", "tasks/..", "tasks//x"):
        with pytest.raises(ValueError):
            cas.ref_set(bad, s1)
    # a crash mid-write must leave the old value intact and no temp file
    real = os.replace

    def boom(a, b):
        raise OSError("crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cas.ref_set("tasks/t1", s1)
    monkeypatch.setattr(os, "replace", real)
    assert cas.ref_get("tasks/t1") == s2 and sorted(x.name for x in p.parent.iterdir()) == ["t1", "t2"]


def test_provenance_in_hash_and_deterministic(ws):
    e = {"a": {"sha256": "0" * 64, "size": 1}}
    kw = dict(task_id="t1", commit="c" * 40, producer=PROD, created_at=PIN, inputs=["b" * 64, "a" * 64], parent=None)
    p1 = cas.build_provenance(**kw)
    assert p1["inputs"] == ["a" * 64, "b" * 64] and p1["created_at"] == PIN
    m1 = cas.put_manifest(e, provenance=p1)
    assert cas.put_manifest(e, provenance=cas.build_provenance(**kw)) == m1  # deterministic
    assert cas.put_manifest(e, provenance=cas.build_provenance(**{**kw, "task_id": "t2"})) != m1
    assert cas.put_manifest(e, provenance=cas.build_provenance(**{**kw, "created_at": "2026-11-06T00:00:00Z"})) != m1
    assert cas.put_manifest(e) != m1
    assert cas.read_manifest(m1)["provenance"] == p1 and cas.read_manifest(m1)["schema"] == 1


def test_commit_moves_refs_and_chains_parent(ws, tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "a").write_bytes(b"1")
    cas.add(d)
    m1 = cas.commit(d, task_id="t9", created_at=PIN)
    assert cas.ref_get(cas.dir_ref(d)) == m1 and cas.ref_get("tasks/t9") == m1
    assert cas.read_manifest(m1)["provenance"]["parent"] is None
    (d / "a").write_bytes(b"2")
    assert cas.status(d)["changes"] == {"a": "modified"}
    cas.add(d)
    m2 = cas.commit(d, task_id="t9", created_at=PIN)
    assert m2 != m1 and cas.read_manifest(m2)["provenance"]["parent"] == m1
    assert cas.ref_get("tasks/t9") == m2 and cas.diff(d) == {}
    (d / "ARTIFACT_MANIFEST.json").unlink()  # ref, not the file, is the source of truth
    assert cas.diff(d) == {}


# ---- logical manifest (schema 2) + derived-ext exclusion ----
def _mixed(tmp_path):
    d = tmp_path / "mix"
    d.mkdir()
    for n in ("a.md", "b.csv", "fig.png", "out.PDF", "deck.pptx"):
        (d / n).write_text(n)
    return d


def test_logical_manifest_is_path_free_and_deterministic(tmp_path):
    d = _mixed(tmp_path)
    arts, sha = cas.logical_manifest(d, meta=lambda rel: {"kind": "doc", "epoch_id": "e1"})
    assert set(arts) == {"a", "b"}
    for e in arts.values():
        assert set(e) == {"sha256", "size_bytes", "kind", "format", "epoch_id", "producer_role"}
    assert arts["b"]["format"] == "csv" and arts["a"]["kind"] == "doc"
    raw = cas.get(sha).decode()
    assert '"path"' not in raw and "relpath" not in raw and "a.md" not in raw and "b.csv" not in raw
    assert json.loads(raw)["schema"] == cas.LOGICAL_SCHEMA == 2
    assert cas.logical_manifest(d, meta=lambda rel: {"kind": "doc", "epoch_id": "e1"})[1] == sha
    assert cas.read_manifest(sha)["artifacts"] == arts


def test_logical_manifest_validation(tmp_path):
    e = cas.logical_entry(cas.put(b"x"), 1)
    for bad in ("", "/abs", "a/../b", "a//b", "a b", "../x"):
        with pytest.raises(ValueError):
            cas.put_logical_manifest({bad: e})
    with pytest.raises(ValueError):
        cas.logical_entry("zz", 1)
    d = tmp_path / "dup"
    d.mkdir()
    (d / "x.md").write_text("1")
    (d / "x.csv").write_text("2")
    with pytest.raises(ValueError):
        cas.logical_manifest(d)
    assert len(cas.logical_manifest(d, artifact_id=lambda r: r.replace(".", "_"))[0]) == 2


def test_write_logical_manifest_and_ref(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    e = cas.logical_entry(cas.put(b"x"), 1)
    sha = cas.write_logical_manifest(d, {"p-1/iter_005/m": e}, ref="projects/p1/epoch_1")
    assert (d / cas.MANIFEST_NAME).read_bytes() == cas.get(sha)
    assert cas.ref_get("projects/p1/epoch_1") == sha


def test_exclude_ext(tmp_path):
    d = _mixed(tmp_path)
    ex = cas.DERIVED_EXTENSIONS
    assert set(cas.manifest(d, exclude_ext=ex)[0]) == {"a.md", "b.csv"}
    assert set(cas.manifest(d, exclude_ext=["PDF", ".Png"])[0]) == {"a.md", "b.csv", "deck.pptx"}
    assert len(cas.manifest(d)[0]) == 5
    assert set(cas.diff(d, exclude_ext=ex)) == {"a.md", "b.csv"}
    assert len(cas.diff(d)) == 5
    assert set(cas.add(d, exclude_ext=ex)) == {"a.md", "b.csv"}
    assert set(cas.status(d, exclude_ext=ex)["unstaged"]) == set()
    assert set(cas.status(d, exclude_ext=ex)["changes"]) == {"a.md", "b.csv"}
