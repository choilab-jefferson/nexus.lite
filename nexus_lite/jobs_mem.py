"""Opt-in per-job memory cap for ``nexus-lite job start --mem-max SIZE`` (parity with xrun's ``--mem-limit``, #1151).

No cap unless asked. When asked, the best available mechanism enforces it, so a runaway job dies ALONE instead of
OOM-killing the machine or container:

1. ``scope``: ``systemd-run --user --scope -p MemoryMax=`` (a hard cgroup cap). Only used when a probe shows systemd
   really applied the limit (read back from the scope's own ``memory.max``). A kernel OOM kill is attributed by the
   increase of ``oom_kill`` in the scope's ``memory.events``, whatever the command's exit code is.
2. ``watchdog``: the job supervisor polls the summed memory (Pss, else RSS) of the job's process group/session and
   kills it when the sum passes the cap (poll: ``$NEXUS_LITE_MEM_POLL`` / ``$XRUN_MEM_POLL`` seconds, default 2; a
   burst shorter than one poll is missed, which is why the cgroup scope is preferred).
3. neither: the job runs unlimited and ``start`` warns on stderr.

Either way the run dir gets a ``memkill`` file (``used_kb=N limit_kb=M`` or ``limit_kb=M oom_kill=N``) and the log
gets a line with "memory limit exceeded"; status shows the detail and the notify verdict of a failed job is ``OOM``.
``$NEXUS_LITE_MEM_ENFORCER`` / ``$XRUN_MEM_ENFORCER`` = ``watchdog`` | ``scope`` forces one rung.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

_SIZE_RE = re.compile(r"^(\d+)([KkMmGgTt]?)$")
_UNIT_KB = {"": 1, "K": 1, "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}
SCOPE_PROPS = ("-p", "MemorySwapMax=0", "-p", "OOMPolicy=continue")
KILL_GRACE_S = 5.0
_LINUX = sys.platform.startswith("linux")
_MEMORY_MAX_PROBE = ("import os; cg = open('/proc/self/cgroup').read().strip().split(':', 2)[2]; "
                     "print(open('/sys/fs/cgroup' + cg + '/memory.max').read().strip())")


def _env(name: str) -> str:
    return os.environ.get(f"NEXUS_LITE_{name}") or os.environ.get(f"XRUN_{name}") or ""


def _machine_limit_kb() -> Optional[int]:
    """The container's memory limit if a cgroup sets one below the machine's memory, else the machine's memory."""
    total = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    if total is None:
        try:
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                       timeout=10).stdout.strip()) // 1024
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        lim = int(Path("/sys/fs/cgroup/memory.max").read_text().strip())
        if lim // 1024 <= total:
            return lim // 1024
    except (OSError, ValueError):
        pass
    return total


def parse_size_kb(value: Optional[str]) -> Optional[int]:
    """``64G`` / ``512M`` / ``N%`` (of the container or machine memory) / ``123`` (KiB) -> KiB; ``None`` for no cap
    (unset, ``0``, ``off``, ``none``). A malformed value raises ValueError."""
    v = (value or "").strip()
    if v.lower() in ("", "0", "off", "none"):
        return None
    m = re.match(r"^(\d+)%$", v)
    if m:
        pct, lim = int(m.group(1)), _machine_limit_kb()
        if not 1 <= pct <= 100 or lim is None:
            raise ValueError(f"bad memory size {value!r}")
        return max(1, lim * pct // 100)
    m = _SIZE_RE.match(v)
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"bad memory size {value!r}")
    return int(m.group(1)) * _UNIT_KB[m.group(2).upper()]


def _scope_works(kb: int) -> bool:
    if not (_LINUX and shutil.which("systemd-run")):
        return False
    try:
        r = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={kb}K", *SCOPE_PROPS, "--",
                            sys.executable, "-c", _MEMORY_MAX_PROBE], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    out = r.stdout.strip()
    return r.returncode == 0 and out.isdigit() and int(out) <= kb * 1024


def pick_enforcer(kb: Optional[int]) -> str:
    """``scope`` | ``watchdog`` | ``none`` for a cap of `kb` KiB."""
    if kb is None:
        return "none"
    want = _env("MEM_ENFORCER")
    if want != "watchdog" and _scope_works(kb):
        return "scope"
    if want == "scope":
        return "none"
    return "watchdog" if (Path("/proc/self/stat").is_file() or shutil.which("ps")) else "none"


# ---------------------------------------------------------------- watchdog
def _proc_members(pgid: int, sid: int, skip: int) -> List[int]:
    out = []
    for e in os.scandir("/proc"):
        if not e.name.isdigit() or int(e.name) == skip:
            continue
        try:
            rest = Path(e.path, "stat").read_text().rsplit(")", 1)[1].split()
            state, pgrp, session = rest[0], int(rest[2]), int(rest[3])
        except (OSError, IndexError, ValueError):
            continue
        if state not in ("Z", "X") and (pgrp == pgid or session == sid):
            out.append(int(e.name))
    return out


def _ps_members(pgid: int, skip: int) -> Dict[int, int]:
    try:
        rows = subprocess.run(["ps", "-axo", "pid=,pgid=,rss="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for ln in rows.splitlines():
        p = ln.split()
        if len(p) == 3 and all(x.isdigit() for x in p) and int(p[1]) == pgid and int(p[0]) != skip:
            out[int(p[0])] = int(p[2])
    return out


def _pid_kb(pid: int) -> int:
    for path, key in ((f"/proc/{pid}/smaps_rollup", "Pss:"), (f"/proc/{pid}/status", "VmRSS:")):
        try:
            for line in Path(path).read_text().splitlines():
                if line.startswith(key):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return 0


def members_kb(pgid: int, sid: int, skip: int) -> Tuple[List[int], int]:
    """(pids, summed KiB) of the job's process group and session, except `skip` (the supervisor itself)."""
    if Path("/proc/self/stat").is_file():
        pids = _proc_members(pgid, sid, skip)
        return pids, sum(_pid_kb(p) for p in pids)
    rows = _ps_members(pgid, skip)
    return list(rows), sum(rows.values())


def _signal(pids: Sequence[int], sig: int) -> None:
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def run_watched(cmd: Union[str, Sequence[str]], cwd: str, limit_kb: int, poll_s: Optional[float] = None,
                on_start: Optional[Callable[[], None]] = None) -> Tuple[int, Optional[Dict[str, int]]]:
    """Run `cmd` as a child of the supervisor (the group leader) and poll the group's memory. Returns (rc, info):
    info is None unless the cap was exceeded, else {used_kb, limit_kb} after the group was TERMed then KILLed."""
    poll = poll_s if poll_s is not None else float(_env("MEM_POLL") or 2)
    me, pgid, sid = os.getpid(), os.getpgid(0), os.getsid(0)
    p = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, stdin=subprocess.DEVNULL)
    if on_start:
        on_start()
    while True:
        try:
            return p.wait(timeout=poll), None
        except subprocess.TimeoutExpired:
            pass
        pids, used = members_kb(pgid, sid, me)
        if used <= limit_kb:
            continue
        print(f"jobs: memory limit exceeded (out of memory guard, OOM): job used {used} KiB > limit {limit_kb} KiB; "
              f"killing the job's process group", flush=True)
        _signal(pids, signal.SIGTERM)
        try:
            p.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
        _signal(members_kb(pgid, sid, me)[0], signal.SIGKILL)
        return p.wait(), {"used_kb": used, "limit_kb": limit_kb}


def supervisor_argv(*args: str) -> List[str]:
    """Command line that runs ``nexus_lite.jobs._main(args)`` in a clean interpreter. ``-I`` ignores PYTHONPATH,
    PYTHON* variables, the user site and the cwd, so neither the caller's environment nor the job cwd can shadow the
    stdlib; lite's own package parent is APPENDED to sys.path (after the stdlib and site-packages), so a source
    checkout works and a site-packages parent (possibly holding stale backports) never precedes the stdlib."""
    parent = str(Path(__file__).resolve().parent.parent)
    boot = f"import sys; sys.path.append({parent!r}); from nexus_lite.jobs import _main; sys.exit(_main(sys.argv[1:]))"
    return [sys.executable, "-I", "-c", boot, *args]


# ---------------------------------------------------------------- scope
def scope_argv(d: Path, limit_kb: int) -> List[str]:
    """The supervisor's command line for the cgroup scope: systemd-run rewrites `$` in its arguments, so the user's
    command never goes through it; the scope runs ``_scoperun`` (below) on the run dir, which holds the command."""
    return ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={limit_kb}K", *SCOPE_PROPS, "--",
            *supervisor_argv("_scoperun", str(d).replace("$", "$$"))]


def _oom_kill_count(events: Path) -> int:
    try:
        for line in events.read_text().splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def scope_run(cmd: Union[str, Sequence[str]], cwd: str, limit_kb: int,
              record: Callable[[str], None], on_start: Optional[Callable[[], None]] = None) -> int:
    """Runs INSIDE the scope as the command's parent: after the command, any increase of the scope's ``oom_kill``
    counts as an OOM (a multi-process job survives when the kernel kills only a child, so rc is not consulted).
    `record(memkill_text)` stores it. Returns the command's exit code (128+N for a signal, like a shell)."""
    try:
        cg = Path("/proc/self/cgroup").read_text().strip().split(":", 2)[2]
    except (OSError, IndexError):
        cg = ""
    events = Path("/sys/fs/cgroup" + cg) / "memory.events"
    before = _oom_kill_count(events)
    try:
        p = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, stdin=subprocess.DEVNULL)
        if on_start:
            on_start()
        rc = p.wait()
    except OSError as e:
        print(f"jobs: cannot run command: {e}", flush=True)
        return 127
    after = _oom_kill_count(events)
    if after > before:
        print(f"jobs: memory limit exceeded (out of memory guard, OOM): job hit the cgroup limit {limit_kb} KiB "
              f"(oom_kill {before} -> {after}); the job was killed alone", flush=True)
        record(f"limit_kb={limit_kb} oom_kill={after}\n")
    return rc if rc >= 0 else 128 - rc
