"""Hermetic tests for nexus_lite.loom.install_loom: no network."""
import hashlib
import io
import os
import subprocess
import sys

import pytest

from nexus_lite import loom

BIN = b"#!/bin/sh\necho fake-loom\n"


class _Resp(io.BytesIO):
    pass


def _serve(monkeypatch, asset, checksum):
    seen = []

    def fake_urlopen(url, timeout=None):
        seen.append(url)
        if url.endswith("/checksums.txt"):
            return _Resp(f"{checksum}  {asset}\n{'0' * 64}  other\n".encode())
        if url.endswith("/" + asset):
            return _Resp(BIN)
        raise AssertionError(url)

    monkeypatch.setattr(loom.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_install_ok(monkeypatch, tmp_path):
    monkeypatch.delenv("LOOM_INSTALL_URL", raising=False)
    asset = loom._asset_name()
    seen = _serve(monkeypatch, asset, hashlib.sha256(BIN).hexdigest())
    dest = tmp_path / "bin" / "loom"
    out = loom.install_loom(dest=dest)
    assert out == dest and dest.read_bytes() == BIN
    assert os.access(dest, os.X_OK)
    assert seen[0].startswith("https://github.com/choilab-jefferson/nexus.loom/releases/download/v0.2.2/")
    assert not list(dest.parent.glob("*.part"))


def test_install_mismatch(monkeypatch, tmp_path):
    _serve(monkeypatch, loom._asset_name(), "f" * 64)
    dest = tmp_path / "loom"
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        loom.install_loom(dest=dest)
    assert not dest.exists() and not list(tmp_path.glob("*.part"))


def test_install_base_url_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOM_INSTALL_URL", "https://mirror.example/x/")
    seen = _serve(monkeypatch, loom._asset_name(), hashlib.sha256(BIN).hexdigest())
    loom.install_loom(dest=tmp_path / "loom")
    assert seen[0] == "https://mirror.example/x/checksums.txt"


@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", "loom-linux-amd64"),
    ("Linux", "aarch64", "loom-linux-arm64"),
    ("Darwin", "x86_64", "loom-darwin-amd64"),
    ("Darwin", "arm64", "loom-darwin-arm64"),
])
def test_asset_selection(system, machine, expected):
    assert loom._asset_name(system, machine) == expected


@pytest.mark.parametrize("system,machine", [("Windows", "AMD64"), ("Linux", "riscv64")])
def test_asset_unsupported(system, machine):
    with pytest.raises(RuntimeError, match="no released loom binary"):
        loom._asset_name(system, machine)


def test_import_has_no_network():
    code = ("import urllib.request as u, socket\n"
            "def boom(*a, **k): raise SystemExit('network at import')\n"
            "u.urlopen = boom; socket.socket.connect = boom\n"
            "import nexus_lite.loom as l; l.LoomAdapter\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.getcwd()})
    assert r.returncode == 0, r.stderr
