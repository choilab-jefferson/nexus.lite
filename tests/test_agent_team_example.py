"""examples/agent_team_bus.py: a planner / builder / runner team with a human lead on one bus
(skills/nexus-agent-team). The example must run from a checkout and show the protocol it documents."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env["NEXUS_LITE_DIR"] = str(tmp_path / ".nexus")
    p = subprocess.run([sys.executable, str(ROOT / "examples" / "agent_team_bus.py"), "--json"],
                       capture_output=True, text=True, env=env, timeout=120, cwd=tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_messages_carry_ids_and_answers_name_what_they_answer(tmp_path):
    out = _run(tmp_path)
    firsts = [m["summary"] for m in out["log"]]
    assert firsts[0].startswith("[P-001] plan ready: plans/PLAN-001.md")
    assert any(s.startswith("[B-001] re: P-001") for s in firsts)
    assert any(s.startswith("[R-001] re: B-002") for s in firsts)


def test_reader_drains_a_backlog_larger_than_one_page(tmp_path):
    out = _run(tmp_path)
    assert out["builder_pages"] > 1 and out["builder_read"] == out["builder_inbox"]


def test_watcher_cursor_is_independent_of_the_reader(tmp_path):
    out = _run(tmp_path)
    assert out["watch_seqs"] == out["builder_seqs"]


def test_relayed_approval_is_confirmed_with_the_human_first(tmp_path):
    out = _run(tmp_path)
    steps = out["builder_actions"]
    assert steps.index("asked lead to confirm P-002") < steps.index("merged after L-001")
    assert "merged on P-002" not in steps
