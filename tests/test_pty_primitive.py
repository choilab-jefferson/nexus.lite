"""Public pty primitives: dtach master-pid resolver and manifest classifier."""
import builtins
import io

import pytest

from nexus_lite import pty as P

SOCK = "/tmp/x.sock"


def _fake_proc(monkeypatch, procs):
    """procs: {pid: (argv, children_str)}"""
    monkeypatch.setattr(P.glob, "glob", lambda pat: [f"/proc/{p}/cmdline" for p in procs])
    real_open = builtins.open

    def fake_open(path, mode="r", *a, **k):
        path = str(path)
        for pid, (argv, kids) in procs.items():
            if path == f"/proc/{pid}/cmdline":
                return io.BytesIO(b"\x00".join(x.encode() for x in argv) + b"\x00")
            if path == f"/proc/{pid}/task/{pid}/children":
                return io.StringIO(kids)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)


def test_no_children_is_none(monkeypatch):
    _fake_proc(monkeypatch, {10: (["dtach", "-A", SOCK], "")})
    assert P.find_dtach_master_pid(SOCK) is None


def test_master_with_children_chosen(monkeypatch):
    _fake_proc(monkeypatch, {
        10: (["dtach", "-A", SOCK], "11"),
        12: (["dtach", "-A", SOCK], ""),
        13: (["python", "--sock", SOCK], "99"),  # non-dtach ignored
    })
    assert P.find_dtach_master_pid(SOCK) == 10


def test_ambiguous_is_none(monkeypatch):
    _fake_proc(monkeypatch, {
        10: (["dtach", "-A", SOCK], "11"),
        12: (["dtach", "-A", SOCK], "13"),
    })
    assert P.find_dtach_master_pid(SOCK) is None


def test_alias_kept():
    assert P._find_dtach_pid is P.find_dtach_master_pid


@pytest.mark.parametrize("prov,text,state", [
    ("claude", "✻ Pondering… (esc to interrupt)", "working"),
    ("claude", "❯ ", "idle"),
    ("agy", "Do you trust the contents of this folder", "needs_input"),
    ("agy", "Generating... esc to cancel", "working"),
    ("gemini", "Do you trust the files in this folder?", "needs_input"),
    ("gemini", "Thinking…", "working"),
    ("copilot", "Do you want to approve this request?", "needs_input"),
    ("copilot", "Working", "working"),
    ("opencode", "Permission required", "needs_input"),
    ("codex", "Would you like to run the following command?", "needs_input"),
    ("codex", "⠋ thinking", "working"),
    ("bash", "user@host:~$", "idle"),
    ("", "⠋ x", "working"),
    ("claude", "   \n", "none"),
])
def test_classify(prov, text, state):
    assert P.classify(text, prov) == state


def test_manifests_and_derived():
    assert {"claude", "agy", "gemini", "copilot", "opencode", "codex", "bash", ""} <= set(P.PROVIDER_MANIFESTS)
    assert P.is_busy("esc to interrupt", "claude") and not P.is_idle("esc to interrupt", "claude")
    assert P.is_idle("❯ ", "claude") and not P.is_busy("❯ ", "bash")


def test_public_packet_aliases_are_private_objects():
    assert P.PKT is P._PKT
    assert P.PUSH_MAX is P._PUSH_MAX
    assert P.packet is P._packet
    assert P.winch_packet is P._winch_packet
