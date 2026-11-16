---
name: nexus-detached-jobs
description: "[Public / Open-Core] Run long or unknown-duration work as detached jobs whose state lives on disk (nexus-lite job ...), observe them with one terminal line instead of a log stream, and make multi-step pipelines resumable with a step ledger and validated sentinels. Use whenever a command may run longer than a couple of minutes, or before handing mechanical multi-step work to another agent."
---

# Detached jobs with nexus-lite

**Failure this prevents.** An agent runs a long script in its own shell (or `nohup` plus a poll loop it runs
itself). The agent hits a rate limit, runs out of context or ends its turn; the run is orphaned or killed, nobody
records the exit code, and the next agent cannot tell "still running" from "died at 70 %".

**Doctrine: the job outlives the agent; the agent observes, it does not host.** Launch work into its own session,
record its state on disk, and read that state. Waiting is not work: wait for a terminal state and read one line,
never the raw log.

## 1. Launch and observe

```bash
nexus-lite job start fit -- python fit.py --config cfg.json     # returns at once
nexus-lite job wait fit --timeout 7200                           # ONE line, exit code from the state
nexus-lite job check fit                                         # verdict as JSON, never the raw log
nexus-lite job digest                                            # the same for every job under the root
nexus-lite job tail fit 40                                       # capped, only for a run flagged by check
```

- States: `OK` / `FAIL` (from the recorded exit code), `RUNNING` (pid whose start time and boot id match),
  `GONE` (died without an exit code, or its pid was reused), `SKIP` (valid sentinel, not launched; the run dir file is `skipped`, the older `skip` is still read),
  `AMBIGUOUS` / `UNKNOWN` (not measurable now: never terminal, never success).
- `wait` exit codes: 0 OK/SKIP, 1 FAIL, 3 not terminal yet, 4 GONE, 2 usage or refusal. `wait` blocks on a kernel
  exit event (Linux `pidfd`, macOS `kqueue`), after checking the pid's start time and boot id; only where there is
  none does it poll (every 1 s).
- `job start NAME --notify TARGET -- CMD`: at the terminal state the supervisor sends ONE bus message of kind `job`
  to TARGET (summary `<name> <verdict> rc=<rc>`, `data` = name, verdict `OK|FAIL|GONE|KILLED|OOM`, rc, run_dir, ts,
  supervisor), sent before the exit code is recorded, so a `job wait` that returned implies the message is on the bus. A waiter then needs only `nexus-lite bus wait --target TARGET`, not a `job wait` poll beside it.
- Root: `--root`, `$NEXUS_LITE_JOBS_ROOT`, else `<workspace>/jobs`. Re-running a name keeps the previous run under
  `<name>/prev/`; a name whose job may still run is refused unless `--force`. The job sees `RUN_DIR` = its run dir
  (an inherited `RUN_DIR` wins; `--own-run-dir` overrides it), which is where `job ledger` writes by default.
- `nexus-lite job log NAME` prints the log path (read it with `check`/`tail`, never follow it).
- Roots index: every `start` records its root once (paths only, no names or logs) in
  `~/.local/share/nexus/artifacts/.xrun-roots` (`$NEXUS_LITE_ROOTS_INDEX` / `$XRUN_ROOTS_INDEX`). `job list --all`
  walks every indexed root; `status|wait|check|tail|log|stop NAME` look in the default root first, then in the
  indexed ones (a name in several roots is refused: pass `--root`). `job roots [--prune]` lists/prunes the index.
- Memory cap (opt-in): `job start NAME --mem-max 64G -- CMD` (also `512M`, `80%`; `$NEXUS_LITE_MEM_MAX`; none by
  default). A systemd user scope with `MemoryMax=` when it really applies, else a watchdog that kills the job's process
  group when its summed memory passes the cap (`$NEXUS_LITE_MEM_ENFORCER=watchdog|scope` forces one; poll
  `$NEXUS_LITE_MEM_POLL`, default 2 s). The job dies alone: run dir gets `memkill`, the log "memory limit exceeded",
  `status` shows `(memory limit exceeded (OOM: ...))`, `check` has `oom` and `mem_max_kb`, and the `job` message
  verdict of a failed job is `OOM`. No mechanism: a stderr warning and the job runs unlimited.
- `wait --xrun-exit-codes`: only OK exits 0; FAIL, GONE and SKIP exit 1; not terminal at the timeout stays 3.

## 2. Resume without redoing work: sentinels

`--sentinel PATH` with `--min-size N` or `--assert CMD` skips the launch when the output already exists AND passes
its check now. A sentinel without a check never skips (existence is not evidence). A failed check turns exit code 0
into FAIL: "the job said OK" must mean something. Write outputs atomically - a temporary file beside the sentinel,
then `nexus-lite job ledger commit TMP SENTINEL` - so a killed step never leaves a complete-looking output.

## 3. Pipelines: one ledger row per step

```bash
nexus-lite job scaffold prep            # writes run_prep.py: preflight, run_step, ledger, resume
# edit the STEPS block, then
nexus-lite job start prep -- python run_prep.py
RESUME=1 nexus-lite job start prep --force -- python run_prep.py    # skips steps whose sentinel validates
```

Each execution appends to its step log under its own header and writes a START and an OK|FAIL row (`#nexus.ledger/2`, tab-separated: utc, step, status, seconds,
log, sentinel, assert); a skipped step writes one SKIP row. Progress mid-flight comes from that ledger, read on
demand, never from following a log. Step logs keep every execution, so an `--assert` that greps `$LOG` also sees earlier
executions: check the sentinel, or only the part after the last `=== step NAME START` header. For a step done by an agent rather than a command:
`ledger begin NAME ...` (exit 0 SKIP, 1 RUN) then `ledger mark NAME OK|FAIL ...`.

## 4. Waiting on something outside (CI, a queue)

```bash
nexus-lite job start ci -- nexus-lite job wait-for --until success,failed,canceled --ok success \
    --pattern 'Pipeline state: (\w+)' --interval 60 --timeout 3600 -- <your status command>
nexus-lite job wait ci --timeout 4000
```

Only the extracted state decides; nothing else in the output does. Interval and timeout have no defaults: choose
them.

## 5. Budget: gate on your own quota

`nexus-lite job load --quota N --need K` counts your own runnable processes against your quota (exit 0 when K cores
are free). The machine load average counts every tenant, so gating on it stalls you on cores that were never yours.
The quota is yours to set (`--quota` or `$NEXUS_LITE_CPU_QUOTA`); without either, `load` refuses (exit 2): the
machine's core count is not your quota on a shared host.

## Anti-patterns

- Running a script in the agent's own shell "just to check" - start a job (a smoke run is a job too).
- Following a log (`tail -f`, re-reading it for progress) - read the ledger or wait for the terminal line.
- Treating an existing output file as done without a check.
- Delegating judgment-free multi-step work to another agent instead of a driver script.
