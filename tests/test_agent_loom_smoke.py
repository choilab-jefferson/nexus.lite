"""Acceptance #10 (opt-in): a real loom binary as an ordinary command. Skipped unless LOOM_BIN or `loom` on PATH.

No loom import and no version pin. The offline `mock` provider makes it deterministic.

Smoke-test constraint: the current loom CLI does not read the worker's stdin as its prompt, so this test uses a
temporary shell wrapper (`-p "$(cat)"`). That is not the final integration of the generic command contract: command
substitution drops trailing newlines, large bodies hit the argv limit, and it adds a quoting layer. Add a small wrapper
or thin adapter only if a real attachment hits the same problem.
"""

import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path

import pytest

from nexus_lite import _db
from nexus_lite import agent_run as ar
from nexus_lite.memory import mtm

LOOM = os.environ.get("LOOM_BIN") or shutil.which("loom")
pytestmark = pytest.mark.skipif(not LOOM, reason="loom not installed (set LOOM_BIN to run)")


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = Path(tempfile.mkdtemp(prefix="nl", dir="/tmp"))
    monkeypatch.setenv("NEXUS_LITE_HOME", str(w))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(w))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(w / "s"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOOM_NO_AGENTS_MD", "1")
    _db.reset_init()
    yield w
    shutil.rmtree(w, ignore_errors=True)


def loom_shell(work: Path, context: bool) -> str:
    cmd = f'{shlex.quote(LOOM)} -p "$(cat)" --provider mock --workspace {shlex.quote(str(work))}'
    return cmd + (" --context-in {context_in} --context-out {context_out}" if context else "")


def replies():
    return [m["content"] for m in sorted(mtm.bus_read(target="alice", limit=20), key=lambda m: m["seq"])]


def test_phase1_three_independent_one_shot_calls(ws):
    (ws / "work").mkdir()
    for i in range(3):
        mtm.bus_send("loomy", f"task {i}", sender="alice")
    assert ar.agent_run("loomy", loom_shell(ws / "work", context=False), shell=True, exit_when_idle=True,
                        pass_env=["LOOM_NO_AGENTS_MD"]) == 3
    assert [r.strip() for r in replies()] == ["mock task complete"] * 3


def test_phase2_three_turns_continue_through_context_placeholders(ws):
    (ws / "work").mkdir()
    for i in range(3):
        mtm.bus_send("loomy", f"turn {i}", sender="alice")
    ar.agent_run("loomy", loom_shell(ws / "work", context=True), shell=True, exit_when_idle=True,
                 pass_env=["LOOM_NO_AGENTS_MD"])
    files = list((ws / "agents" / "loomy" / "context").iterdir())
    assert len(files) == 1
    users = [m["content"] for m in json.loads(files[0].read_text())["messages"] if m["role"] == "user"]
    assert users == ["turn 0", "turn 1", "turn 2"]
