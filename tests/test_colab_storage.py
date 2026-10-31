"""Colab storage layout. Hermetic: fake Colab env, no Drive, no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from nexus_lite import bridge, core, pty


@pytest.fixture
def fake_colab(tmp_path, monkeypatch):
    drive = tmp_path / "content" / "drive" / "MyDrive"
    drive.mkdir(parents=True)
    local = tmp_path / "content" / ".nexus"
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: True if str(p) == "/content/drive/MyDrive" else real_exists(p))
    monkeypatch.setattr(core, "COLAB_DRIVE_DIR", str(drive / "nexus-lite"))
    monkeypatch.setenv("NEXUS_LITE_LOCAL_DIR", str(local))
    monkeypatch.delenv("NEXUS_LITE_SOCK_DIR", raising=False)
    monkeypatch.setattr(core.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(core, "sync_dotdir_to_drive", lambda *a, **k: None)
    return drive, local


def test_colab_db_not_on_drive(fake_colab):
    drive, local = fake_colab
    core.ensure_colab(mount_drive=True, install_opencode=False)
    assert not str(core.get_db_path()).startswith("/content/drive")
    assert core.get_db_path().parent == local.resolve()
    assert os.environ["NEXUS_LITE_SOCK_DIR"].startswith("/tmp/nexus-lite-")


def test_colab_sync_artifacts_only(fake_colab):
    drive, local = fake_colab
    core.ensure_colab(mount_drive=True, install_opencode=False)
    (local / "artifacts" / "t1").mkdir(parents=True)
    (local / "artifacts" / "t1" / "a.txt").write_text("x")
    assert "artifacts" in core.colab_sync_to_drive()
    dd = drive / "nexus-lite"
    assert (dd / "artifacts" / "t1" / "a.txt").read_text() == "x"
    assert not (dd / "lite.db").exists()


def test_long_socket_path_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / ("d" * 110)))
    with pytest.raises(ValueError, match="socket path too long"):
        pty.sock_path("sess")


def test_foreign_pid_bridge_json_not_signalled(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path))
    core.init(ws)
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (ws / "bridge.json").write_text(json.dumps(
            {"pid": victim.pid, "nonce": "stale", "local_url": "http://127.0.0.1:9"}))
        bridge.stop_bridge()
        assert victim.poll() is None
        assert not (ws / "bridge.json").exists()
    finally:
        victim.kill()
        victim.wait()
