"""`bus wait`: block until the target has an unacked message (push-style watching; the agent runs it as a background
job and is woken when it exits). Nothing is acked by waiting."""
import os
import subprocess
import sys
import threading
import time

import pytest

from nexus_lite import _db
from nexus_lite.memory import mtm


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(tmp_path / "socks"))
    _db.reset_init()
    return w


def test_returns_when_a_message_arrives_and_acks_nothing():
    t = threading.Timer(0.2, lambda: mtm.bus_send("bob", "hello", sender="al"))
    t.start()
    t0 = time.monotonic()
    got = mtm.bus_wait("bob", timeout=5, poll_s=0.02)
    t.join()
    assert [m["sender"] for m in got] == ["al"] and time.monotonic() - t0 < 4
    # not acked: the next wait returns at once; after the ack it times out
    assert len(mtm.bus_wait("bob", timeout=0.1, poll_s=0.02)) == 1
    mtm.bus_ack("bob", got[0]["seq"])
    assert mtm.bus_wait("bob", timeout=0.1, poll_s=0.02) == []


def test_other_targets_and_since_seq_do_not_wake():
    first = mtm.bus_send("bob", "old", sender="al")
    mtm.bus_send("carol", "not yours", sender="al")
    seq = mtm.bus_read(target="bob", unacked=True)[0]["seq"]
    assert first and mtm.bus_wait("bob", timeout=0.1, poll_s=0.02, since_seq=seq) == []


def test_cli_exit_codes(ws):
    env = dict(os.environ, NEXUS_LITE_HOME=str(ws), NEXUS_LITE_DIR=str(ws))
    cmd = [sys.executable, "-m", "nexus_lite", "bus", "wait", "--target", "bob", "--poll-s", "0.05"]
    r = subprocess.run(cmd + ["--timeout", "0.2"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 124 and "timeout" in r.stdout
    mtm.bus_send("bob", "ping", sender="al")
    r = subprocess.run(cmd + ["--timeout", "5", "--full"], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "seq=" in r.stdout and "ping" in r.stdout
