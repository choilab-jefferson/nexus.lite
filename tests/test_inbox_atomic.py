"""Inbox bodies are published atomically: written to a temp file in the same dir, then renamed into place."""

import os
import stat

from nexus_lite import _db
from nexus_lite import artifacts as A
from nexus_lite.memory import mtm


def test_replace_private_writes_0600_and_leaves_no_temp(tmp_path):
    p = A.replace_private(tmp_path / "m.md", "hello")
    assert p.read_text() == "hello"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert sorted(x.name for x in tmp_path.iterdir()) == ["m.md"]


def test_replace_private_replaces_existing_file(tmp_path):
    (tmp_path / "m.md").write_text("old")
    A.replace_private(tmp_path / "m.md", b"new")
    assert (tmp_path / "m.md").read_text() == "new"


def test_replace_private_never_exposes_a_partial_file(tmp_path, monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst):
        with open(src) as f:
            seen.append((os.path.exists(dst), f.read()))
        return real(src, dst)

    monkeypatch.setattr(A.os, "replace", spy)
    A.replace_private(tmp_path / "m.md", "complete body")
    assert seen == [(False, "complete body")]  # the final name appears only with the full body


def test_failed_write_leaves_no_temp_and_no_target(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(A.os, "replace", boom)
    try:
        A.replace_private(tmp_path / "m.md", "x")
    except OSError:
        pass
    assert list(tmp_path.iterdir()) == []


def test_bus_send_publishes_inbox_body_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "s"))
    _db.reset_init()
    renamed = []
    real = os.replace
    monkeypatch.setattr(A.os, "replace", lambda s, d: (renamed.append(str(d)), real(s, d))[1])
    r = mtm.bus_send("worker", "body text", sender="alice")
    assert [d for d in renamed if "/inbox/" in d] == [str(r["path"])]  # other stores use os.replace too
    assert [p.name for p in (tmp_path / "inbox").iterdir()] == [os.path.basename(r["path"])]
