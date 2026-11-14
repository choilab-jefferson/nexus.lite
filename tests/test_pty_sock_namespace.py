"""PTY runtime sockets are workspace-scoped: <ws>/run by default, /tmp/nx-<workspace hash> when that path is too long.

An explicit NEXUS_LITE_SOCK_DIR still wins. spawn, lookup, pointer injection and kill all resolve through
pty.get_sock_dir(), so two workspaces with the same agent name never share a socket.
"""

import hashlib
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

import pytest

from nexus_lite import _db, pty
from nexus_lite.memory import mtm


@pytest.fixture
def short_ws():
    made = []

    def mk():
        w = Path(tempfile.mkdtemp(prefix="nl", dir="/tmp")).resolve()
        made.append(w)
        return w

    yield mk
    for w in made:
        shutil.rmtree(w, ignore_errors=True)


def use_ws(monkeypatch, w):
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.delenv("NEXUS_LITE_SOCK_DIR", raising=False)
    _db.reset_init()


def test_explicit_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    assert pty.get_sock_dir() == str(tmp_path / "socks")


def test_default_is_workspace_run_dir_0700(monkeypatch, short_ws):
    w = short_ws()
    use_ws(monkeypatch, w)
    assert pty.get_sock_dir() == str(w / "run")
    assert stat.S_IMODE(os.stat(w / "run").st_mode) == 0o700
    assert pty.sock_path("worker") == str(w / "run" / "nexus-lite-worker.sock")


def test_long_workspace_falls_back_to_hashed_tmp_dir(monkeypatch, tmp_path):
    w = (tmp_path / ("d" * 80)).resolve()
    w.mkdir()
    use_ws(monkeypatch, w)
    want = "/tmp/nx-" + hashlib.sha256(str(w).encode()).hexdigest()[:12]
    try:
        assert pty.get_sock_dir() == want
        assert stat.S_IMODE(os.stat(want).st_mode) == 0o700
        assert len(pty.sock_path("a" * 40).encode()) <= 103
    finally:
        shutil.rmtree(want, ignore_errors=True)


def test_never_a_plain_shared_tmp_dir(monkeypatch, tmp_path, short_ws):
    for w in (short_ws(), (tmp_path / ("e" * 80))):
        Path(w).mkdir(exist_ok=True)
        use_ws(monkeypatch, Path(w).resolve())
        d = pty.get_sock_dir()
        assert d not in ("/tmp", "/private/tmp")
        if d.startswith("/tmp/nx-"):
            shutil.rmtree(d, ignore_errors=True)


def test_fallback_dir_owned_by_someone_else_is_refused(monkeypatch, tmp_path):
    w = (tmp_path / ("f" * 80)).resolve()
    w.mkdir()
    use_ws(monkeypatch, w)
    want = "/tmp/nx-" + hashlib.sha256(str(w).encode()).hexdigest()[:12]
    os.symlink(str(tmp_path), want)  # a pre-planted link must not be followed
    try:
        with pytest.raises(RuntimeError):
            pty.get_sock_dir()
    finally:
        os.unlink(want)


def test_two_workspaces_same_agent_name_are_isolated(monkeypatch, short_ws):
    a, b = short_ws(), short_ws()
    use_ws(monkeypatch, a)
    pty.spawn(["cat"], name="worker")
    use_ws(monkeypatch, b)
    pty.spawn(["cat"], name="worker")
    try:
        assert pty.sock_path("worker") != str(a / "run" / "nexus-lite-worker.sock")
        mtm.bus_send("worker", "only-for-b", sender="alice")  # pointer injected in workspace b only
        time.sleep(0.5)
        assert "only-for-b" in pty.read("worker")
        use_ws(monkeypatch, a)
        assert "only-for-b" not in pty.read("worker")
    finally:
        for w in (a, b):
            use_ws(monkeypatch, w)
            pty.kill("worker")


def test_drive_fuse_workspace_falls_back_to_hashed_tmp_dir(monkeypatch):
    for prefix in ("/content/drive/ws", "/mnt/drive/ws", "/Volumes/GoogleDrive/ws"):
        use_ws(monkeypatch, Path(prefix))
        want = "/tmp/nx-" + hashlib.sha256(prefix.encode()).hexdigest()[:12]
        assert pty.default_sock_dir() == want
        assert pty._on_drive_fuse(prefix) is True


def test_af_unix_eopnotsupp_falls_back_to_hashed_tmp_dir(monkeypatch, short_ws):
    import errno
    import socket as real_socket

    w = short_ws()
    use_ws(monkeypatch, w)
    pty._SOCK_DIR_CAPABLE.pop(str(w / "run"), None)

    class _NoUnixSock:
        def bind(self, _addr):
            raise OSError(errno.EOPNOTSUPP, "Operation not supported")

        def close(self):
            pass

    class _FakeSockMod:
        AF_UNIX = real_socket.AF_UNIX
        SOCK_STREAM = real_socket.SOCK_STREAM

        @staticmethod
        def socket(*_a, **_k):
            return _NoUnixSock()

    monkeypatch.setattr(pty, "socket", _FakeSockMod)
    try:
        want = "/tmp/nx-" + hashlib.sha256(str(w).encode()).hexdigest()[:12]
        assert pty.default_sock_dir() == want
    finally:
        pty._SOCK_DIR_CAPABLE.pop(str(w / "run"), None)
