import json
import stat

import pytest

from nexus_lite import _db, cas, locations as loc


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    _db.reset_init()
    _db.init(w)
    return w


S1, S2 = "a" * 64, "b" * 64


def test_record_and_lookup(ws):
    loc.record("p/i1/m", S1, "out/m.md")
    assert loc.by_artifact_id("p/i1/m") == {"sha256": S1, "locations": {"workspace": "out/m.md"}}
    assert loc.by_sha256(S1) == {"workspace": ["out/m.md"]}
    assert loc.by_artifact_id("nope") is None and loc.by_sha256(S2) == {}


def test_same_sha_two_ids(ws):
    loc.record_many([("p/a", S1, "a.md"), ("p/b", S1, "b.md")])
    assert loc.by_sha256(S1) == {"workspace": ["a.md", "b.md"]}


def test_rerecord_new_sha_prunes_old(ws):
    loc.record("p/a", S1, "a.md")
    loc.record("p/a", S2, "a.md")
    assert loc.by_sha256(S1) == {}
    assert loc.by_sha256(S2) == {"workspace": ["a.md"]}
    assert loc.by_artifact_id("p/a")["sha256"] == S2
    assert S1 not in loc.load()["by_sha256"]


def test_labels_coexist_and_forget(ws):
    loc.record("p/a", S1, "a.md")
    loc.record("p/a", S1, "mirror/a.md", label="local_copy")
    assert loc.by_artifact_id("p/a")["locations"] == {"workspace": "a.md", "local_copy": "mirror/a.md"}
    loc.forget("p/a", label="local_copy")
    assert loc.by_sha256(S1) == {"workspace": ["a.md"]}
    loc.forget("p/a")
    assert loc.by_artifact_id("p/a") is None and loc.load()["by_sha256"] == {}


def test_validation(ws):
    for bad in ("/abs", "../x", "a/../b", ""):
        with pytest.raises(ValueError):
            loc.record("p/a", S1, bad)
    with pytest.raises(ValueError):
        loc.record("bad id", S1, "a")
    with pytest.raises(ValueError):
        loc.record("p/a", "xyz", "a")


def test_default_path_mode_and_override(ws, tmp_path):
    loc.record("p/a", S1, "a.md")
    p = ws / "cas" / "ARTIFACT_LOCATIONS.json"
    assert p.is_file() and stat.S_IMODE(p.stat().st_mode) == 0o600
    other = tmp_path / "elsewhere" / "loc.json"
    loc.record("p/z", S2, "z.md", path=other)
    assert json.loads(other.read_text())["by_artifact_id"]["p/z"]["sha256"] == S2
    assert loc.by_artifact_id("p/z") is None  # default file untouched


def test_corrupt_file_gives_skeleton(ws):
    (ws / "cas").mkdir(exist_ok=True)
    (ws / "cas" / "ARTIFACT_LOCATIONS.json").write_text("{not json")
    assert loc.load() == {"schema": 1, "by_sha256": {}, "by_artifact_id": {}}
    loc.record("p/a", S1, "a.md")
    assert loc.by_sha256(S1) == {"workspace": ["a.md"]}
