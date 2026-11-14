---
name: nexus-session-handoff
description: "[Public / Open-Core] Hand an agent session over to a fresh session when context is long, when the human lead asks, or when prolonged idle polling triggers standby (idle_handoff_recommended == true); write one self-contained handoff file, announce [HANDOFF] on the nexus-lite bus, stop all wakeup timers, and enter standby."
---

# Session Handoff & Long-Idle Standby (`nexus-session-handoff`)

Any agent on a `nexus-lite` team bus (`planner`, `builder`, `runner`, etc.) can hand off to a fresh session so a new session can continue cold from a single file without asking anyone.

## When to Trigger

1. **Human Lead Request or Long Context**: The human lead asks to hand off, or the session context window is getting full, or the session is closing with work in flight.
2. **Prolonged Idle Standby (`idle_handoff_recommended == true`)**: The session has been polling the bus with zero actionable messages and no active background run for 2+ hours (`idle_seconds >= 7200` or `consecutive_empty >= 12` in `adaptive_bus_poll.py`). Instead of continuing empty polling turns indefinitely, write the handoff file, announce `[HANDOFF]`, stop all wakeup timers, and wait in standby.
3. **Safety Boundary**: Never hand off in the middle of a write (finish or abort any in-flight file edit, commit, or bus reply first).

## A. Outgoing Session Steps

1. **Collect Verifiable State from Disk**:
   - Repository `HEAD` commits and `git status -sb` for any checkouts you touch.
   - Active background runs/jobs (phase, completed steps, running PIDs).
   - Bus watch state (`.nexus/watch_state/<agent>.json`, last outbound message ID, any open requests).
2. **Write `handoff/HANDOFF_<agent>_<YYYYMMDD>T<HHMM>Z.md`** with:
   1. Role and standing rules for this agent.
   2. Workspace paths and repository locations.
   3. Bus identity, next outbound message ID, and `nexus-bus-adaptive-watch` command.
   4. Current verified state (repos, runs, open MRs/issues).
   5. Ordered next steps with exact commands.
   6. Parked items awaiting human lead decisions.
3. **Announce on the Bus**:
   `[HANDOFF] <agent> -> new session; file handoff/HANDOFF_<...>.md; next id <n>; open: <one-line summary>`
4. **Stop All Wakeup Timers & Enter Standby**:
   Do not schedule any further wakeup timer. If an already-queued timer fires, reply `"handed off (standby)"` in one line and do not reschedule.

## B. Incoming Session Steps

1. Read the latest `HANDOFF_<agent>_*.md` file and re-verify repository/run state on disk before acting.
2. Resume the bus watch (`adaptive_bus_poll.py --agent <agent> --reset-fast`), continue the message ID sequence, and post `[HANDOFF-ACK] <agent> new session active` on the bus.
