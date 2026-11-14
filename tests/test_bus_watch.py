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


def test_before_and_while_oldest_first_once():
    for i in range(3):
        mtm.bus_send("bob", f"pre{i}", sender="al")
    t = threading.Timer(0.15, lambda: mtm.bus_send("bob", "live", sender="al"))
    t.start()
    got = [m["content"] if "content" in m else m["summary"] for m in mtm.bus_watch("bob", poll_s=0.02, timeout=0.6)]
    t.join()
    assert len(got) == 4
    assert got[-1].startswith("live")
    seqs = [m["seq"] for m in mtm.bus_watch("bob", poll_s=0.02, timeout=0.1, ack=False)]
    assert seqs == []  # all acked by first watch


def test_independent_cursors():
    mtm.bus_send("bob", "x", sender="al")
    assert len(list(mtm.bus_watch("bob", poll_s=0.02, timeout=0.1))) == 1
    assert len(mtm.bus_read(target="bob", unacked=True)) == 1
    assert len(mtm.bus_read(target="bob", unacked=True, consumer="turn")) == 1


def test_timeout_prompt():
    t0 = time.monotonic()
    assert list(mtm.bus_watch("bob", poll_s=0.05, timeout=0.2)) == []
    assert time.monotonic() - t0 <= 0.2 + 0.05 + 0.1


def test_ack_false_replays_on_new_watch():
    mtm.bus_send("bob", "x", sender="al")
    a = list(mtm.bus_watch("bob", poll_s=0.02, timeout=0.1, ack=False))
    b = list(mtm.bus_watch("bob", poll_s=0.02, timeout=0.1, ack=False))
    assert len(a) == len(b) == 1


def test_callback_form_and_stop():
    mtm.bus_send("bob", "x", sender="al")
    seen = []
    ev = threading.Event()
    n = mtm.bus_watch("bob", poll_s=0.02, callback=lambda m: (seen.append(m), ev.set()), stop=ev)
    assert n == 1 and len(seen) == 1
