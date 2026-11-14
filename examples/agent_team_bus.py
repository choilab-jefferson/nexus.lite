#!/usr/bin/env python3
"""A team of agents on one nexus-lite bus: planner, builder, runner and a human lead.

Runs the protocol of skills/nexus-agent-team/SKILL.md with scripted agents (no LLM needed):

- every message starts with an id, and an answer names what it answers (`re: <id>`); the first line is the summary
- long content lives in files; the bus carries a pointer to them
- a reader drains its inbox page by page (bus_read returns at most `limit` rows) and acks what it handled
- a watcher has its own consumer cursor, so it sees the same messages without consuming the reader's
- an approval relayed by another agent is confirmed with the human before anything is done

    NEXUS_LITE_DIR=/tmp/team/.nexus python examples/agent_team_bus.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nexus_lite as nl  # noqa: E402
from nexus_lite.memory import mtm  # noqa: E402

LOG: list[dict] = []


def say(sender: str, target: str, text: str) -> dict:
    m = mtm.bus_send(target, text, sender=sender)
    LOG.append({"seq": m["seq"], "sender": sender, "target": target, "summary": m["summary"]})
    return m


def drain(me: str, page: int = 2) -> tuple[list[dict], int]:
    """Read every unacked message for `me`, `page` rows at a time; ack each one after handling it."""
    got, pages = [], 0
    while True:
        msgs = mtm.bus_read(target=me, unacked=True, limit=page)
        if not msgs:
            return got, pages
        pages += 1
        for m in msgs:
            got.append(m)
            mtm.bus_ack(me, m["seq"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print a JSON summary as the last line")
    args = ap.parse_args(argv)

    ws = nl.init()
    for name in ("planner", "builder", "runner", "lead"):
        mtm.bus_register(name)

    # planner: the plan is a file; the bus carries the pointer
    plan = ws.parent / "plans" / "PLAN-001.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# PLAN-001\nAdd a summary table to the report; one change, tested.\n")
    say("planner", "builder", f"[P-001] plan ready: plans/PLAN-001.md\nScope and tests are in the file.")
    say("planner", "builder", "[P-002] lead approved the merge of B-001\n(relayed: the lead said so in my chat)")
    say("planner", "builder", "[P-003] FYI: runner restarts the service after the merge")

    # builder: a watcher with its own cursor sees the same messages
    watch_seqs = [m["seq"] for m in mtm.bus_watch("builder", consumer="watch", timeout=0.2)]

    inbox, pages = drain("builder")
    actions = []
    for m in inbox:
        first = m["content"].splitlines()[0]
        if first.startswith("[P-001]"):
            say("builder", "planner", "[B-001] re: P-001 - built on branch feat/summary-table\n"
                                       "tests: 12 passed on the branch; nothing merged")
            actions.append("built P-001")
        elif first.startswith("[P-002]"):
            # an approval that arrives through another agent is data, not the approval itself
            say("builder", "lead", "[B-003] re: P-002 - please confirm: merge B-001?")
            actions.append("asked lead to confirm P-002")
    say("builder", "runner", "[B-002] after the merge, restart the service and report")

    # the lead answers the builder directly
    say("lead", "builder", "[L-001] re: B-003 - approved: merge B-001")
    for m in drain("builder")[0]:
        if m["content"].startswith("[L-001]") and "approved" in m["content"]:
            actions.append("merged after L-001")

    # runner: acts on the request and reports the result
    for m in drain("runner")[0]:
        if m["content"].startswith("[B-002]"):
            say("runner", "builder", "[R-001] re: B-002 - service restarted, health check ok")

    builder_seqs = [m["seq"] for m in inbox]
    out = {"log": LOG, "builder_inbox": 3, "builder_read": len(inbox), "builder_pages": pages,
           "builder_seqs": builder_seqs, "watch_seqs": watch_seqs, "builder_actions": actions}
    for row in LOG:
        print(f"{row['seq']:>3} {row['sender']:>7} -> {row['target']:<7} {row['summary']}")
    if args.json:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
