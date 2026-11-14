"""Detached jobs: long or unknown-duration work runs outside any agent's context, and its state lives on disk.

A job runs in its own session and process group with stdin closed, so it survives the agent that started it; any
later agent or script reads its state from the run directory without having watched it. Waiting is not work: wait
for a terminal state and read one line, never the raw log.

Run directory ``<root>/<name>/``: ``meta.json`` (command, cwd, sentinel, assertion), ``pid``, ``pgid``, ``pidstart``,
``boot_id``, ``started``, ``ended``, ``rc``, ``assert`` and ``log``. Root: ``root=`` argument, then
``$NEXUS_LITE_JOBS_ROOT``, then ``<workspace>/jobs`` (``NEXUS_LITE_DIR``), so a team's jobs stay in its workspace.

Liveness, answered once:

1. A recorded exit code is ground truth: rc 0 -> OK, else FAIL. A configured assertion that fails turns rc 0 into
   FAIL ("the job said OK" must mean something).
2. Without rc, the pid decides - but a pid is the job only if its start time and the boot id match what the job
   recorded (pids are reused): match -> RUNNING; alive but different -> GONE (never signalled); dead -> GONE.
3. Nothing measurable -> UNKNOWN. UNKNOWN and AMBIGUOUS are never terminal and never read as success.
4. A valid resume sentinel means the job was not launched: SKIP.

Re-running a name never deletes the previous run: its files move to ``<name>/prev/<stamp>/`` (all kept unless
``keep`` is set). A name whose job is not terminal (RUNNING with another command, AMBIGUOUS, UNKNOWN) is refused
unless ``force``.

Limit: if the ``_wrap`` group leader itself is SIGKILLed, its command may keep running in the same process group
while status says GONE (no rc, leader dead) and ``stop`` refuses (it signals only a verified identity, by design);
end such a group by hand.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from ._db import get_workspace

RUNNING, OK, FAIL, GONE, SKIP, AMBIGUOUS, UNKNOWN = "RUNNING", "OK", "FAIL", "GONE", "SKIP", "AMBIGUOUS", "UNKNOWN"
TERMINAL = (OK, FAIL, GONE, SKIP)
TAIL_MAX = 200
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ERROR_RE = re.compile(r"(?i)\b(error|exception|traceback|fatal)\b")


class JobError(RuntimeError):
    pass


@dataclass
class Status:
    name: str
    state: str
    rc: Optional[int] = None
    source: str = "none"          # rc | pid | sentinel | none
    detail: str = ""
    started: Optional[float] = None
    ended: Optional[float] = None
    log_idle_s: Optional[float] = None
    meta: Dict = field(default_factory=dict)

    def line(self) -> str:
        """One line for a human or an agent: state, rc, runtime, detail."""
        parts = [self.name, self.state]
        if self.rc is not None:
            parts.append(f"rc={self.rc}")
        if self.started:
            parts.append(f"runtime={round((self.ended or time.time()) - self.started)}s")
        if self.state == RUNNING and self.log_idle_s is not None:
            parts.append(f"log_idle={round(self.log_idle_s)}s")
        if self.detail:
            parts.append(f"({self.detail})")
        return " ".join(parts)


# ---------------------------------------------------------------- roots and names
def default_root() -> Path:
    env = os.environ.get("NEXUS_LITE_JOBS_ROOT")
    return Path(env).resolve() if env else (get_workspace() / "jobs").resolve()


def _root(root: Optional[Union[str, Path]]) -> Path:
    return Path(root).resolve() if root else default_root()


def _dir(name: str, root) -> Path:
    if not isinstance(name, str) or not NAME_RE.match(name) or name in (".", ".."):
        raise JobError(f"job name must match {NAME_RE.pattern} (no '/', no leading '.'): {name!r}")
    return _root(root) / name


# ---------------------------------------------------------------- process identity
def pid_alive(pid: int) -> bool:
    """True if a process with this pid exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    state = _proc_state(pid)
    return state not in ("Z", "X")


def _proc_state(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return ""
    out = _run(["ps", "-o", "stat=", "-p", str(pid)])
    return out[:1] if out else ""


def pid_start(pid: int) -> str:
    """The process start time as the kernel reports it (Linux: /proc stat field 22; elsewhere: ps lstart)."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return ""
    return _run(["ps", "-o", "lstart=", "-p", str(pid)], env={"LC_ALL": "C"})


def boot_id() -> str:
    p = Path("/proc/sys/kernel/random/boot_id")
    if p.is_file():
        return p.read_text().strip()
    out = _run(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return m.group(1) if m else out


def _run(argv: List[str], env: Optional[Dict[str, str]] = None) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                           env={**os.environ, **(env or {})})
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


# ---------------------------------------------------------------- sentinel and assertion
def _assertion(meta: Dict, rc: Optional[int] = None, log: Optional[Path] = None) -> Optional[str]:
    """None if the sentinel passes the configured assertion, else why not. Existence-only without an assertion."""
    sentinel = meta.get("sentinel")
    if not sentinel:
        return None
    sp = Path(sentinel)
    if not sp.exists():
        return f"sentinel {sentinel} missing"
    if meta.get("min_size") is not None and sp.stat().st_size < int(meta["min_size"]):
        return f"sentinel {sentinel} smaller than {meta['min_size']} bytes"
    if meta.get("assert"):
        env = {**os.environ, "SENTINEL": str(sp), "LOG": str(log or ""), "RC": "" if rc is None else str(rc)}
        r = subprocess.run(meta["assert"], shell=True, env=env, cwd=meta.get("cwd") or None,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if r.returncode != 0:
            return f"assert `{meta['assert']}` exited {r.returncode}"
    return None


def _asserted(meta: Dict) -> bool:
    return bool(meta.get("sentinel")) and (meta.get("min_size") is not None or bool(meta.get("assert")))


# ---------------------------------------------------------------- start
def start(name: str, cmd: Union[str, Sequence[str]], *, root=None, cwd: Optional[str] = None,
          env: Optional[Dict[str, str]] = None, sentinel: Optional[str] = None, assert_cmd: Optional[str] = None,
          min_size: Optional[int] = None, force: bool = False, keep: Optional[int] = None) -> Dict:
    """Launch `cmd` detached and return at once: {"state", "detail", "dir"}; state is the verified status after the
    launch (RUNNING only with a verified pid; UNKNOWN if neither pid nor rc appeared), SKIP or "already started".

    A string `cmd` runs through the shell, a list without one. A live job of the same name is "already started" when
    its command and cwd are the same and refused otherwise (unless `force`); a job that is not terminal (AMBIGUOUS,
    UNKNOWN) is refused unless `force`. The previous run moves to ``prev/`` (``keep``: how many previous runs to keep;
    None keeps all). With `sentinel`, a valid sentinel skips the launch; a stale one (fails `min_size` /
    `assert_cmd`) is re-run."""
    d = _dir(name, root)
    if keep is not None and (isinstance(keep, bool) or not isinstance(keep, int) or keep < 0):
        raise JobError(f"keep must be an integer >= 0, got {keep!r}")
    if min_size is not None and assert_cmd:
        raise JobError("use either min_size or assert_cmd, not both")
    if (min_size is not None or assert_cmd) and not sentinel:
        raise JobError("min_size / assert_cmd need a sentinel")
    cwd = str(Path(cwd or os.getcwd()).resolve())
    argv = cmd if isinstance(cmd, str) else [str(c) for c in cmd]
    meta = {"name": name, "cmd": argv, "cwd": cwd, "sentinel": str(Path(sentinel).resolve()) if sentinel else None,
            "assert": assert_cmd, "min_size": min_size, "env_keys": sorted(env or {}), "created": time.time(),
            "run_id": uuid.uuid4().hex}
    if d.exists():
        cur = status(name, root=root)
        if cur.state == RUNNING:
            if cur.meta.get("cmd") == argv and cur.meta.get("cwd") == cwd:
                return {"state": "already started", "dir": str(d)}
            if not force:
                raise JobError(f"job {name} is RUNNING with another command or cwd; stop it or use force")
            stop(name, root=root)  # a forced restart of a verified run ends it first
        elif cur.state not in TERMINAL and _has_run(d) and not force:
            raise JobError(f"job {name} is {cur.state} ({cur.detail}); it may still run: check it or use force")
    if sentinel and _assertion(meta) is None:
        _reset(d, keep)
        _write(d / "meta.json", json.dumps(meta, indent=1))
        _write(d / "skip", f"sentinel {meta['sentinel']} valid")
        return {"state": SKIP, "dir": str(d)}
    _reset(d, keep)
    _write(d / "meta.json", json.dumps(meta, indent=1))
    job_env = {**os.environ, **(env or {})}
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    job_env["PYTHONPATH"] = pkg_parent + (os.pathsep + job_env["PYTHONPATH"] if job_env.get("PYTHONPATH") else "")
    _spawn_launcher(d, job_env, cwd)
    t_end = time.monotonic() + START_WAIT_S
    while time.monotonic() < t_end:
        if (d / "pid").is_file() or (d / "rc").is_file():
            break
        time.sleep(0.05)
    st = status(name, root=root)
    if st.state == UNKNOWN:
        st.detail = f"no pid or rc within {START_WAIT_S:g} s of the launch"
    return {"state": st.state, "detail": st.detail, "dir": str(d)}


START_WAIT_S = 10.0  # how long start() waits for the job to record its pid
RUN_FILES = ("pid", "pgid", "pidstart", "boot_id", "started", "ended", "rc", "assert", "skip", "log", "meta.json")


def _spawn_launcher(d: Path, env: Dict[str, str], cwd: str) -> None:
    # an intermediate launcher exits at once, so the job is not our child (no zombie that looks alive)
    subprocess.run([sys.executable, "-m", "nexus_lite.jobs", "_spawn", str(d)], env=env, cwd=cwd,
                   stdin=subprocess.DEVNULL, check=True)


def _has_run(d: Path) -> bool:
    return any((d / f).exists() for f in RUN_FILES)


def _reset(d: Path, keep: Optional[int] = None) -> None:
    """Move the previous run's files to ``prev/<stamp>/`` (never deleted here); with `keep`, drop the oldest
    previous runs beyond `keep`."""
    d.mkdir(parents=True, exist_ok=True)
    present = [f for f in RUN_FILES if (d / f).exists()]
    if present:
        t = _read(d / "started") or _read(d / "ended")
        try:
            when = float(t) if t else (d / "meta.json").stat().st_mtime
        except (ValueError, OSError):
            when = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(when)) + f".{int(when * 1000) % 1000:03d}Z"
        dest, n = d / "prev" / stamp, 1
        while dest.exists():
            dest, n = d / "prev" / f"{stamp}-{n}", n + 1
        dest.mkdir(parents=True)
        for f in present:
            os.replace(d / f, dest / f)
    if keep is not None and (d / "prev").is_dir():
        import shutil
        old = sorted((d / "prev").iterdir(), key=lambda q: q.name)
        for q in old[:max(0, len(old) - keep)]:
            shutil.rmtree(q)


def _spawn(d: Path) -> None:
    log = open(d / "log", "ab", buffering=0)
    subprocess.Popen([sys.executable, "-m", "nexus_lite.jobs", "_wrap", str(d)], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)


def _wrap(d: Path) -> int:
    """Runs as the session/group leader: record identity, run the command, record assertion then rc."""
    meta = json.loads((d / "meta.json").read_text())
    me = os.getpid()
    # identity first, pid last: whoever sees `pid` also sees what makes it this job
    _write(d / "pidstart", pid_start(me))
    _write(d / "boot_id", boot_id())
    _write(d / "pgid", str(os.getpgid(0)))
    _write(d / "started", str(time.time()))
    _write(d / "pid", str(me))
    cmd = meta["cmd"]
    try:
        rc = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=meta["cwd"], stdin=subprocess.DEVNULL).returncode
    except OSError as e:
        print(f"jobs: cannot run command: {e}", flush=True)
        rc = 127
    # a forced restart may have moved this run to prev/ while it ran: record into the directory that holds this run
    home = _home_of(d, meta.get("run_id"))
    if home is None:
        print(f"jobs: run {meta.get('run_id')} ended rc={rc} but its directory is gone; not recorded", flush=True)
        return 0
    if _asserted(meta):
        why = _assertion(meta, rc, home / "log")
        _write(home / "assert", "PASS" if why is None else f"FAIL {why}")
    _write(home / "ended", str(time.time()))
    _write(home / "rc", str(rc))
    return 0


def _run_id(d: Path) -> Optional[str]:
    try:
        return json.loads((d / "meta.json").read_text()).get("run_id")
    except (OSError, ValueError):
        return None


def _home_of(d: Path, run_id: Optional[str]) -> Optional[Path]:
    """The directory whose meta.json carries `run_id`: the job dir, or one of its prev/ runs."""
    if _run_id(d) == run_id:
        return d
    prev = d / "prev"
    for q in (sorted(prev.iterdir()) if prev.is_dir() else []):
        if _run_id(q) == run_id:
            return q
    return None


# ---------------------------------------------------------------- status, wait, stop
def status(name: str, *, root=None) -> Status:
    st = _status_once(name, root)
    if st.state in (GONE, AMBIGUOUS) and (_dir(name, root) / "rc").is_file():
        # the job finished while we probed its pid (an exiting process has no start time): the rc decides
        st = _status_once(name, root)
    return st


def _status_once(name: str, root) -> Status:
    d = _dir(name, root)
    meta = {}
    if (d / "meta.json").is_file():
        try:
            meta = json.loads((d / "meta.json").read_text())
        except ValueError:
            meta = {}
    num = lambda f: float(_read(d / f)) if _read(d / f) else None  # noqa: E731
    st = Status(name=name, state=UNKNOWN, started=num("started"), ended=num("ended"), meta=meta)
    if not d.is_dir():
        st.detail = "no such job"
        return st
    if (d / "skip").is_file():
        st.state, st.source, st.detail = SKIP, "sentinel", _read(d / "skip") or ""
        return st
    rc = _read(d / "rc")
    if rc is not None and rc.lstrip("-").isdigit():
        st.rc, st.source = int(rc), "rc"
        verdict = _read(d / "assert")
        if verdict and verdict.startswith("FAIL"):
            st.state, st.detail = FAIL, "assert failed: " + verdict[5:]
        else:
            st.state = OK if st.rc == 0 else FAIL
        return st
    pid = _read(d / "pid")
    if not (pid and pid.isdigit()):
        st.detail = "no rc and no pid recorded"
        return st
    st.source = "pid"
    if not pid_alive(int(pid)):
        st.state, st.detail = GONE, f"pid={pid} gone without an rc (killed?)"
        return st
    probes = (("pidstart", pid_start(int(pid))), ("boot_id", boot_id()))
    unread = [f for f, now in probes if not now]
    if unread:  # alive but unmeasurable right now: not terminal, never success, never signalled
        st.state, st.detail = AMBIGUOUS, f"pid={pid} alive but {', '.join(unread)} unreadable"
        return st
    mismatch = [f for f, now in probes if (_read(d / f) or "") != now]
    if mismatch:
        st.state, st.detail = GONE, f"pid={pid} reused by another process ({', '.join(mismatch)} mismatch)"
        return st
    st.state = RUNNING
    try:
        st.log_idle_s = time.time() - (d / "log").stat().st_mtime
    except OSError:
        pass
    return st


def wait(name: str, *, root=None, timeout: Optional[float] = None, poll_s: float = 0.2) -> Status:
    """Block until the job reaches a terminal state (or `timeout`); return its status."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        st = status(name, root=root)
        if st.state in TERMINAL or (deadline is not None and time.monotonic() >= deadline):
            return st
        time.sleep(poll_s)


def stop(name: str, *, root=None, grace_s: float = 5.0) -> Status:
    """TERM then KILL the job's process group - only when its identity is verified (state RUNNING)."""
    st = status(name, root=root)
    if st.state != RUNNING:
        raise JobError(f"refusing to signal {name}: {st.state} ({st.detail})")
    d = _dir(name, root)
    pid, pgid = int(_read(d / "pid")), int(_read(d / "pgid") or 0)
    leads = pgid == pid

    def send(sig):
        try:
            os.killpg(pgid, sig) if leads else os.kill(pid, sig)
        except ProcessLookupError:
            pass
    send(signal.SIGTERM)
    t0 = time.monotonic()
    while time.monotonic() - t0 < grace_s and pid_alive(pid):
        time.sleep(0.1)
    send(signal.SIGKILL)
    return status(name, root=root)


# ---------------------------------------------------------------- reading results
def tail(name: str, n: int = 40, *, root=None) -> List[str]:
    """The last `n` log lines, at most TAIL_MAX - for flagged runs; read check() first."""
    n = max(0, min(int(n), TAIL_MAX))
    try:
        lines = (_dir(name, root) / "log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:] if n else []


def check(name: str, *, root=None) -> Dict:
    """A deterministic verdict without the raw log: state, rc, assertion, runtime, line count, error lines, the first
    error and the most frequent error signature (digits folded to N, cut to 160 characters)."""
    st = status(name, root=root)
    d = _dir(name, root)
    try:
        lines = (d / "log").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    sigs = [_signature(ln) for ln in lines if ERROR_RE.search(ln)]
    counts: Dict[str, int] = {}
    for sig in sigs:
        counts[sig] = counts.get(sig, 0) + 1
    top = max(counts, key=counts.get) if counts else None  # ties: the first seen (dict keeps insertion order)
    return {"name": name, "state": st.state, "rc": st.rc, "detail": st.detail, "assert": _read(d / "assert"),
            "runtime_s": round((st.ended or time.time()) - st.started, 1) if st.started else None,
            "lines": len(lines), "error_lines": len(sigs), "first_error": sigs[0] if sigs else None,
            "top_error": {"signature": top, "count": counts[top]} if top else None}


def _signature(line: str) -> str:
    return re.sub(r"\d+", "N", line.strip())[:160]


def digest(names: Optional[Sequence[str]] = None, *, root=None) -> List[Dict]:
    """check() for each named job, or for every job under the root."""
    return [check(n, root=root) for n in (names or [st.name for st in list_jobs(root=root)])]


# ---------------------------------------------------------------- wait-for
WAITFOR_LAST_MAX = 200  # characters of the observed state shown in wait-for's result (display limit)


def wait_for(cmd: Union[str, Sequence[str]], *, until: Sequence[str], ok: Sequence[str] = (),
             pattern: Optional[str] = None, interval: float, timeout: float) -> Dict:
    """Wait on an external thing (a CI pipeline, a queue) without a context window: run `cmd` every `interval`
    seconds and read its state - the first group of `pattern` in its output, else its whole stripped output - until
    the state is one of `until` or `timeout` passes. {state (or TIMEOUT), ok (state in `ok`), polls, seconds, last}.
    Only the state is read; nothing else in the output decides (a job row reading success inside a running pipeline
    is not the pipeline's state)."""
    rx = re.compile(pattern) if pattern else None
    t0, polls, last = time.monotonic(), 0, None
    while True:
        polls += 1
        remaining = max(0.1, timeout - (time.monotonic() - t0))
        try:
            out = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=remaining).stdout
        except (OSError, subprocess.SubprocessError) as e:
            out = f"<{type(e).__name__}>"
        m = rx.search(out) if rx else None
        last = (m.group(1) if m and m.groups() else m.group(0) if m else None) if rx else out.strip()
        if last is not None:  # a display cap, so the one line never carries a whole output into a context
            last = (last.splitlines() or [""])[0][:WAITFOR_LAST_MAX]
        if last in until:
            return {"state": last, "ok": last in ok, "polls": polls,
                    "seconds": round(time.monotonic() - t0, 1), "last": last}
        if time.monotonic() - t0 + interval > timeout:
            return {"state": "TIMEOUT", "ok": False, "polls": polls,
                    "seconds": round(time.monotonic() - t0, 1), "last": last}
        time.sleep(interval)


# ---------------------------------------------------------------- load
def _ps_rows(user: str) -> Optional[str]:
    try:
        return subprocess.run(["ps", "-u", user, "-o", "pid=,ppid=,stat="], capture_output=True, text=True,
                              timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _proc_rows() -> str:
    """The same rows from /proc (slim Linux images have no ps), for our uid."""
    uid, out = os.getuid(), []
    for d in Path("/proc").iterdir() if Path("/proc/self").exists() else []:
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != uid:
                continue
            stat = (d / "stat").read_text()
        except OSError:
            continue
        rest = stat[stat.rfind(")") + 1:].split()
        if len(rest) >= 2:
            out.append(f"{d.name} {rest[1]} {rest[0]}")
    return "\n".join(out)


def _runnable(user: str) -> int:
    """Our processes in state R (running or runnable): instantaneous demand (ps %CPU is a lifetime average). This
    process and its own children (the ps that measures) are not counted."""
    rows = _ps_rows(user)
    if rows is None:
        rows = _proc_rows()
    me, n = os.getpid(), 0
    for ln in rows.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            if int(parts[0]) == me or int(parts[1]) == me:
                continue
            n += parts[2].startswith("R")
    return n


def load(quota: Optional[int] = None, *, user: Optional[str] = None, need: int = 1) -> Dict:
    """How many cores WE use against OUR quota (not the machine's load average, which counts every tenant):
    {used, quota, free, runnable, ok}; ok when free >= need. Quota: the argument or $NEXUS_LITE_CPU_QUOTA, nothing
    else (on a shared host the machine's cores are not our quota)."""
    if quota is None:
        env = os.environ.get("NEXUS_LITE_CPU_QUOTA")
        if not env:
            raise JobError("no CPU quota: pass --quota or set NEXUS_LITE_CPU_QUOTA")
        quota = int(env)
    import getpass
    used = _runnable(user or getpass.getuser())
    free = max(0, quota - used)
    return {"used": used, "quota": quota, "free": free, "runnable": used, "ok": free >= need}


def list_jobs(*, root=None) -> List[Status]:
    r = _root(root)
    if not r.is_dir():
        return []
    return [status(p.name, root=r) for p in sorted(r.iterdir())
            if p.is_dir() and NAME_RE.match(p.name)]


EXIT = {OK: 0, SKIP: 0, FAIL: 1, GONE: 4}  # anything else (RUNNING, AMBIGUOUS, UNKNOWN): 3; usage or refusal: 2


def _ledger_cli(a, cmd: List[str]) -> int:
    from . import ledger as led
    try:
        if a.lverb == "commit":
            led.commit(a.tmp, a.sentinel)
            return 0
        path = a.ledger or (Path(os.environ["RUN_DIR"]) / "status.tsv" if os.environ.get("RUN_DIR") else None)
        if path is None:
            raise led.LedgerError("no ledger: pass --ledger or set RUN_DIR")
        kw = {"sentinel": a.sentinel, "assert_cmd": a.assert_cmd, "min_size": a.min_size}
        if a.lverb == "step":
            if not cmd:
                raise led.LedgerError("no command: ledger step NAME [opts] -- CMD...")
            r = led.step(path, a.name, cmd, log=a.log or Path(path).parent / f"{a.name}.log", no_skip=a.no_skip,
                         **kw)
            print(f"[{a.name}] {r['status']} rc={r['rc']} assert={r['assert']}")
            return 0 if r["status"] in ("OK", "SKIP") else 1
        if a.lverb == "begin":
            d = led.begin(path, a.name, log=a.log, no_skip=a.no_skip, **kw)
            print(f"[{a.name}] {d}")
            return 0 if d == "SKIP" else 1
        status = led.mark(path, a.name, a.want, log=a.log, **kw)
        print(f"[{a.name}] {status}")
        return 0 if status == "OK" else 1
    except led.LedgerError as e:
        print(f"nexus-lite job ledger: {e}", file=sys.stderr)
        return 2


def cli_main(argv: List[str]) -> int:
    """`nexus-lite job VERB ...`."""
    import argparse
    ap = argparse.ArgumentParser(prog="nexus-lite job", description="Detached jobs whose state lives on disk.")
    ap.add_argument("--root", help="run root (default: $NEXUS_LITE_JOBS_ROOT or <workspace>/jobs)")
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("start", help="launch detached and return at once: start NAME [opts] -- CMD...")
    s.add_argument("name")
    s.add_argument("--cwd")
    s.add_argument("--env", action="append", default=[], metavar="K=V")
    s.add_argument("--sentinel")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--assert", dest="assert_cmd", metavar="CMD")
    g.add_argument("--min-size", type=int)
    s.add_argument("--force", action="store_true")
    s.add_argument("--keep", type=int, default=None, help="previous runs to keep under prev/ (default: all)")
    s.add_argument("--shell", action="store_true", help="run CMD as one shell command line")
    st = sub.add_parser("status", help="one line per job")
    st.add_argument("name", nargs="?")
    w = sub.add_parser("wait", help="block until terminal; print ONE line; exit code from the state")
    w.add_argument("name")
    w.add_argument("--timeout", type=float)
    sub.add_parser("check", help="deterministic verdict as JSON (no raw log)").add_argument("name")
    wf = sub.add_parser("wait-for", help="poll CMD until its state is terminal; ONE line; exit 0 ok / 1 not ok / "
                                      "3 timeout: wait-for --until A,B [--ok A] [--pattern RE] --interval S "
                                      "--timeout S -- CMD...")
    wf.add_argument("--until", required=True)
    wf.add_argument("--ok", default="")
    wf.add_argument("--pattern")
    wf.add_argument("--interval", type=float, required=True)
    wf.add_argument("--timeout", type=float, required=True)
    lg = sub.add_parser("ledger", help="step ledger: ledger step|begin|mark|commit (one row per step event)")
    lsub = lg.add_subparsers(dest="lverb", required=True)
    for verb, hlp in (("step", "run a shell step: SKIP on a validated sentinel, else START, run, OK|FAIL"),
                      ("begin", "skip decision for an agent step: exit 0 SKIP, exit 1 RUN (START row written)"),
                      ("mark", "terminal row OK|FAIL for an agent step (OK with a failed assertion is FAIL)")):
        lp = lsub.add_parser(verb, help=hlp)
        lp.add_argument("name")
        if verb == "mark":
            lp.add_argument("want", choices=("OK", "FAIL"))
        lp.add_argument("--ledger", help="ledger file (default: $RUN_DIR/status.tsv; no other default)")
        lp.add_argument("--sentinel")
        lp.add_argument("--assert", dest="assert_cmd")
        lp.add_argument("--min-size", type=int)
        lp.add_argument("--log")
        if verb != "mark":
            lp.add_argument("--no-skip", action="store_true")
    lc = lsub.add_parser("commit", help="atomic output: fsync TMP, rename onto SENTINEL (same directory)")
    lc.add_argument("tmp")
    lc.add_argument("sentinel")
    sc = sub.add_parser("scaffold", help="write a driver script (ledger, validated-sentinel resume, quota gate)")
    sc.add_argument("name")
    sc.add_argument("--out")
    sc.add_argument("--outdir", default="out")
    sc.add_argument("--force", action="store_true", help="overwrite an existing driver")
    ld = sub.add_parser("load", help="our runnable processes against our CPU quota; exit 0 if free >= --need")
    ld.add_argument("--quota", type=int)
    ld.add_argument("--user")
    ld.add_argument("--need", type=int, default=1)
    sub.add_parser("digest", help="check for each named job (default: all), one JSON line each").add_argument(
        "names", nargs="*")
    t = sub.add_parser("tail", help=f"last N log lines (at most {TAIL_MAX})")
    t.add_argument("name")
    t.add_argument("n", nargs="?", type=int, default=40)
    sub.add_parser("stop", help="TERM then KILL the job's group, only if its identity is verified").add_argument("name")
    sub.add_parser("list", help="one line per job under the root")
    cmd: List[str] = []
    if "--" in argv:  # everything after the first `--` is the job's command, untouched
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    try:
        if a.verb == "start":
            if not cmd:
                raise JobError("no command: start NAME [opts] -- CMD...")
            env = dict(kv.split("=", 1) for kv in a.env)
            r = start(a.name, " ".join(cmd) if a.shell else cmd, root=a.root, cwd=a.cwd, env=env,
                      sentinel=a.sentinel, assert_cmd=a.assert_cmd, min_size=a.min_size, force=a.force, keep=a.keep)
            print(f"{a.name} {r['state']} {r['dir']}" + (f" ({r['detail']})" if r.get("detail") else ""))
            return 3 if r["state"] in (UNKNOWN, AMBIGUOUS) else 0
        if a.verb in ("status", "list"):
            rows = [status(a.name, root=a.root)] if getattr(a, "name", None) else list_jobs(root=a.root)
            for row in rows:
                print(row.line())
            return 0
        if a.verb == "wait":
            row = wait(a.name, root=a.root, timeout=a.timeout)
            print(row.line())
            return EXIT.get(row.state, 3)
        if a.verb == "check":
            print(json.dumps(check(a.name, root=a.root)))
            return 0
        if a.verb == "ledger":
            return _ledger_cli(a, cmd)
        if a.verb == "scaffold":
            from . import ledger as led
            try:
                out = led.scaffold(a.name, a.out or f"run_{a.name}.py", a.outdir, force=a.force)
            except led.LedgerError as e:
                print(f"nexus-lite job scaffold: {e}", file=sys.stderr)
                return 2
            print(f"wrote {out} (edit the STEPS block, then: nexus-lite job start {a.name} -- python {out})")
            return 0
        if a.verb == "wait-for":
            if not cmd:
                raise JobError("no command: wait-for [opts] -- CMD...")
            r = wait_for(cmd, until=[x for x in a.until.split(",") if x], ok=[x for x in a.ok.split(",") if x],
                         pattern=a.pattern, interval=a.interval, timeout=a.timeout)
            print(f"WAITFOR {r['state']} ok={r['ok']} polls={r['polls']} seconds={r['seconds']} last={r['last']}")
            return 0 if r["ok"] else (3 if r["state"] == "TIMEOUT" else 1)
        if a.verb == "load":
            r = load(a.quota, user=a.user, need=a.need)
            print(f"used={r['used']} quota={r['quota']} free={r['free']} runnable={r['runnable']}")
            return 0 if r["ok"] else 1
        if a.verb == "digest":
            for row in digest(a.names, root=a.root):
                print(json.dumps(row))
            return 0
        if a.verb == "tail":
            print("\n".join(tail(a.name, a.n, root=a.root)))
            return 0
        if a.verb == "stop":
            print(stop(a.name, root=a.root).line())
            return 0
    except JobError as e:
        print(f"nexus-lite job: {e}", file=sys.stderr)
        return 2
    return 2


def _main(argv: List[str]) -> int:
    if len(argv) == 2 and argv[0] == "_spawn":
        _spawn(Path(argv[1]))
        return 0
    if len(argv) == 2 and argv[0] == "_wrap":
        return _wrap(Path(argv[1]))
    print("internal entry point; use `nexus-lite job ...`", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
