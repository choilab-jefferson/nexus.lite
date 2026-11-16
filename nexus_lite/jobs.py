"""Detached jobs: long or unknown-duration work runs outside any agent's context, and its state lives on disk.

A job runs in its own session and process group with stdin closed, so it survives the agent that started it; any
later agent or script reads its state from the run directory without having watched it. Waiting is not work: wait
for a terminal state and read one line, never the raw log.

Run directory ``<root>/<name>/``: ``meta.json`` (command, cwd, sentinel, assertion), ``pid``, ``pgid``, ``pidstart``,
``boot_id``, ``started``, ``ended``, ``rc``, ``assert``, ``log``, ``skipped`` (sentinel valid, not launched; the older
name ``skip`` is still read) and ``memkill`` (the opt-in memory cap was exceeded; see ``jobs_mem``). Root: ``root=`` argument, then
``$NEXUS_LITE_JOBS_ROOT``, then ``<workspace>/jobs`` (``NEXUS_LITE_DIR``), so a team's jobs stay in its workspace.
Every ``start`` records its root once in a small index (``jobs_roots``), so ``list --all`` and ``status NAME`` find
runs started from other roots.

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

import calendar
import contextlib
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from . import jobs_mem, jobs_roots
from ._db import get_bus_dir, get_workspace

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
    oom: bool = False             # the memory cap was exceeded (a `memkill` file in the run dir)
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


def _ts(text: Optional[str]) -> Optional[float]:
    """A recorded time: epoch seconds (this engine) or ``YYYY-MM-DDTHH:MM:SSZ`` (xrun run dirs); None if neither."""
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
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
          min_size: Optional[int] = None, force: bool = False, keep: Optional[int] = None,
          notify: Optional[str] = None, mem_max: Optional[str] = None, own_run_dir: bool = False) -> Dict:
    """Launch `cmd` detached and return at once: {"state", "detail", "dir"}; state is the verified status after the
    launch (RUNNING only with a verified pid; UNKNOWN if neither pid nor rc appeared), SKIP or "already started".

    A string `cmd` runs through the shell, a list without one. A live job of the same name is "already started" when
    its command and cwd are the same and refused otherwise (unless `force`); a job that is not terminal (AMBIGUOUS,
    UNKNOWN) is refused unless `force`. The previous run moves to ``prev/`` (``keep``: how many previous runs to keep;
    None keeps all). With `sentinel`, a valid sentinel skips the launch; a stale one (fails `min_size` /
    `assert_cmd`) is re-run. With `notify=TARGET`, the supervisor sends ONE `job` bus message to TARGET when the job
    reaches a terminal state (file bus spec 13.6): `data` = name, verdict, rc, run_dir, ts, supervisor, job_id.
    With `mem_max` (``64G``, ``512M``, ``80%``; no cap by default) the job is held to that memory by a cgroup scope or
    a watchdog (see ``jobs_mem``); an exceeded cap is recorded as ``memkill`` and reported as OOM. The job sees
    ``RUN_DIR`` = its run dir unless the caller already has one (`own_run_dir` forces it; `env` wins over both)."""
    d = _dir(name, root)
    try:
        mem_kb = jobs_mem.parse_size_kb(mem_max)
    except ValueError:
        raise JobError(f"bad mem_max {mem_max!r} (want e.g. 64G, 512M, 80%, off)") from None
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
    if mem_kb is not None:
        enforcer = jobs_mem.pick_enforcer(mem_kb)
        if enforcer == "none":
            print("nexus-lite job: WARNING: no memory-cap mechanism (no systemd scope, no /proc or ps); the job runs "
                  "unlimited", file=sys.stderr)
        meta["mem"] = {"max_kb": mem_kb, "enforcer": enforcer}
    if notify is not None:
        from . import filebus
        filebus.check_name("notify target", notify)
        meta["notify"] = {"target": notify, "bus_dir": str(get_bus_dir()), "workspace": str(get_workspace())}
    if not jobs_roots.add(_root(root)):
        print("nexus-lite job: WARNING: could not record the run root in the roots index", file=sys.stderr)
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
        _write(d / "skipped", meta["sentinel"])
        return {"state": SKIP, "dir": str(d)}
    _reset(d, keep)
    _write(d / "meta.json", json.dumps(meta, indent=1))
    job_env = {**os.environ, **(env or {})}
    if "RUN_DIR" not in (env or {}) and (own_run_dir or not job_env.get("RUN_DIR")):
        job_env["RUN_DIR"] = str(d)
    # the job's environment is the caller's plus `env`; lite finds itself through supervisor_argv, never PYTHONPATH
    _spawn_launcher(d, job_env)
    t_end = time.monotonic() + START_WAIT_S
    while time.monotonic() < t_end:
        # `launched` = the command was exec'd in its cwd (or `rc` = it could not be); only then may a caller remove the cwd
        if (d / "launched").is_file() or (d / "rc").is_file():
            break
        time.sleep(0.05)
    st = status(name, root=root)
    if st.state == UNKNOWN:
        st.detail = f"no pid or rc within {START_WAIT_S:g} s of the launch"
    return {"state": st.state, "detail": st.detail, "dir": str(d)}


START_WAIT_S = 10.0  # how long start() waits for the job command to be exec'd
RUN_FILES = ("pid", "pgid", "pidstart", "boot_id", "started", "ended", "rc", "assert", "skipped", "skip", "memkill",
             "log", "meta.json", "notified", "launched")


def _spawn_launcher(d: Path, env: Dict[str, str]) -> None:
    # an intermediate launcher exits at once, so the job is not our child (no zombie that looks alive); it runs from
    # the run dir, not the job cwd, so nothing in the job cwd can shadow a module the supervisor imports
    subprocess.run(jobs_mem.supervisor_argv("_spawn", str(d)), env=env, cwd=str(d), stdin=subprocess.DEVNULL, check=True)


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
            when = _ts(t) or (d / "meta.json").stat().st_mtime
        except OSError:
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
    subprocess.Popen(jobs_mem.supervisor_argv("_wrap", str(d)), start_new_session=True,
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
    mem = meta.get("mem") or {}
    home_of = lambda: _home_of(d, meta.get("run_id")) or d  # noqa: E731
    launched = lambda: _write(home_of() / "launched", str(time.time()))  # noqa: E731
    try:
        if mem.get("enforcer") == "scope":
            # `_scoperun` inside the scope writes `launched` once the command is exec'd
            rc = subprocess.run(jobs_mem.scope_argv(d, mem["max_kb"]), cwd=str(d), stdin=subprocess.DEVNULL).returncode
        elif mem.get("enforcer") == "watchdog":
            rc, hit = jobs_mem.run_watched(cmd, meta["cwd"], mem["max_kb"], on_start=launched)
            if hit:
                _write(home_of() / "memkill", f"used_kb={hit['used_kb']} limit_kb={hit['limit_kb']}\n")
        else:
            # Popen returns only after the command was exec'd (a bad cwd or command raises here)
            p = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=meta["cwd"], stdin=subprocess.DEVNULL)
            launched()
            rc = p.wait()
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
    # Ordering contract: the `job` message is on the bus BEFORE rc makes the run terminal, so a `job wait` that
    # returns implies the notification has already been sent (or failed and is retried by that waiter).
    if home == d and meta.get("notify"):
        _notify_terminal(d, meta, rc=rc, home=home)
    _write(home / "rc", str(rc))
    return 0


def _scoperun(d: Path) -> int:
    """Runs inside the cgroup scope of `--mem-max` (see ``jobs_mem.scope_argv``): run the command, record an OOM."""
    meta = json.loads((d / "meta.json").read_text())
    home_of = lambda: _home_of(d, meta.get("run_id")) or d  # noqa: E731
    return jobs_mem.scope_run(meta["cmd"], meta["cwd"], meta["mem"]["max_kb"],
                              lambda txt: _write(home_of() / "memkill", txt),
                              on_start=lambda: _write(home_of() / "launched", str(time.time())))


# ---------------------------------------------------------------- terminal notification (file bus `job` kind)
def _verdict(st: "Status") -> str:
    if st.state == GONE:
        return "GONE"
    if st.oom and st.state != OK:
        return "OOM"  # the memory cap killed the job
    if st.state in (OK, FAIL) and st.rc is not None and st.rc < 0:
        return "KILLED"  # the command died on a signal
    return st.state


@contextlib.contextmanager
def _env(**kv: str):
    saved = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _notify_terminal(d: Path, meta: Dict, rc: Optional[int] = None, home: Optional[Path] = None) -> bool:
    """Send the one `job` message of this run (spec 13.6). A marker file written first (O_EXCL) makes the sender
    unique per run; a failed send removes it so a later observer (`wait`) may retry. Never raises. The supervisor
    passes the command's `rc` (not yet recorded) and sends BEFORE writing the rc file; a later observer passes
    none and derives the verdict from the recorded state."""
    n = meta.get("notify")
    if not isinstance(n, dict) or not n.get("target"):
        return False
    marker = d / "notified"
    try:
        os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return False  # already sent (or no dir): at most once per observer
    try:
        if rc is not None:  # the supervisor's own observation: rc 0 -> OK unless the assertion failed, signal -> KILLED
            asserted_fail = (_read((home or d) / "assert") or "").startswith("FAIL")
            verdict = "FAIL" if asserted_fail else ("KILLED" if rc < 0 else ("OK" if rc == 0 else "FAIL"))
            if verdict != "OK" and ((home or d) / "memkill").is_file():
                verdict = "OOM"
        else:
            st = status(d.name, root=d.parent)
            if st.state not in TERMINAL or st.state == SKIP:
                marker.unlink(missing_ok=True)
                return False
            verdict = _verdict(st)
            rc = st.rc if verdict != "GONE" else None
        from . import _db
        from .memory import mtm
        data = {"name": meta["name"], "verdict": verdict, "rc": rc, "run_dir": str(d.resolve()),
                "ts": time.time(), "supervisor": "nexus-lite", "job_id": meta.get("run_id")}
        # the bus lives where `start` recorded it, whatever this process's cwd or env says
        with _env(NEXUS_LITE_DIR=n["workspace"], NEXUS_LITE_HOME=n["workspace"], NEXUS_BUS_DIR=n["bus_dir"]):
            _db.reset_init()
            try:
                mtm.bus_send(n["target"], "", sender=f"job:{meta['name']}", kind="job", data=data,
                             summary=f"{meta['name']} {verdict} rc={'null' if rc is None else rc}")
            finally:
                _db.reset_init()
        return True
    except Exception as e:  # content and argv never go to the log
        marker.unlink(missing_ok=True)
        print(f"jobs: notify failed: {type(e).__name__}", flush=True)
        return False


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
    num = lambda f: _ts(_read(d / f))  # noqa: E731
    st = Status(name=name, state=UNKNOWN, started=num("started"), ended=num("ended"), meta=meta,
                oom=(d / "memkill").is_file())
    if not d.is_dir():
        st.detail = "no such job"
        return st
    for fname in ("skipped", "skip"):  # `skipped` is the name xrun and this engine write; `skip` is read for old runs
        if (d / fname).is_file():
            txt = _read(d / fname) or ""
            st.state, st.source, st.detail = SKIP, "sentinel", f"sentinel={txt}" if fname == "skipped" else txt
            return st
    rc = _read(d / "rc")
    if rc is not None and rc.lstrip("-").isdigit():
        st.rc, st.source = int(rc), "rc"
        verdict = _read(d / "assert")
        if verdict and verdict.startswith("FAIL"):
            st.state, st.detail = FAIL, "assert failed: " + verdict[5:]
        else:
            st.state = OK if st.rc == 0 else FAIL
        if st.oom:
            lim = _read(d / "memkill") or ""
            st.detail = (st.detail + "; " if st.detail else "") + f"memory limit exceeded (OOM: {lim})"
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


def _block_until_exit(pid: int, d: Path, timeout: float) -> bool:
    """Block up to `timeout` s on a kernel exit event for `pid` (Linux: pidfd + select; macOS/BSD: kqueue
    NOTE_EXIT), after verifying that the pid still carries the start time and boot id the job recorded (pids are
    reused). True if the kernel mechanism was used (the caller re-reads the status), False if there is none
    (fallback: poll)."""
    want = (_read(d / "pidstart") or "", _read(d / "boot_id") or "")
    timeout = max(timeout, 0.0)
    try:
        if hasattr(os, "pidfd_open"):
            fd = os.pidfd_open(pid)
            try:
                if (pid_start(pid), boot_id()) != want:
                    return False
                select.select([fd], [], [], timeout)
            finally:
                os.close(fd)
            return True
        if hasattr(select, "kqueue") and hasattr(select, "KQ_NOTE_EXIT"):
            kq = select.kqueue()
            try:
                ev = select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                   fflags=select.KQ_NOTE_EXIT)
                kq.control([ev], 0, 0)
                if (pid_start(pid), boot_id()) != want:
                    return False
                kq.control(None, 1, timeout)
            finally:
                kq.close()
            return True
    except (OSError, ValueError, OverflowError):
        return False
    return False


WAIT_EVENT_CHUNK_S = 60.0  # longest single kernel wait; the status is re-read after each


def wait(name: str, *, root=None, timeout: Optional[float] = None, poll_s: float = 1.0) -> Status:
    """Block until the job reaches a terminal state (or `timeout`); return its status. A RUNNING job is waited on
    through the kernel (pidfd / kqueue, no polling); only where that is unavailable the status is polled every
    `poll_s`. A terminal job whose start asked for `notify` and was not announced yet (its supervisor died) sends
    its one `job` message here."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        st = status(name, root=root)
        if st.state in TERMINAL or (deadline is not None and time.monotonic() >= deadline):
            if st.state in TERMINAL and st.state != SKIP and st.meta.get("notify"):
                d = _dir(name, root)
                if not (d / "notified").exists():
                    _notify_terminal(d, st.meta)
            return st
        left = None if deadline is None else max(deadline - time.monotonic(), 0.0)
        pid = _read(_dir(name, root) / "pid")
        if st.state == RUNNING and pid and pid.isdigit():
            t0 = time.monotonic()
            if _block_until_exit(int(pid), _dir(name, root), WAIT_EVENT_CHUNK_S if left is None else min(left, WAIT_EVENT_CHUNK_S)):
                if time.monotonic() - t0 < 0.01:
                    time.sleep(0.01)  # an instant event that did not end the run (zombie race): do not spin
                continue
        time.sleep(poll_s if left is None else min(poll_s, left))


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
            "oom": st.oom, "mem_max_kb": (st.meta.get("mem") or {}).get("max_kb"),
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


def list_all() -> List[tuple]:
    """Every root in the roots index (plus the default root) as (root, [Status] or None when the root is gone)."""
    roots = [str(default_root())] if default_root().is_dir() else []
    roots += [r for r in jobs_roots.indexed_roots() if r not in roots]
    return [(r, list_jobs(root=r) if Path(r).is_dir() else None) for r in roots]


def locate(name: str, root=None) -> Path:
    """The root a read command (status, wait, check, tail, log, stop) should use for NAME: an explicit `root`, else the
    default root when it holds NAME, else the one indexed root that does. Several indexed roots holding NAME are
    refused (pass `root`); none found falls back to the default root (a clear "no such job")."""
    _dir(name, root)  # validates the name
    if root:
        return _root(root)
    r = default_root()
    if (r / name).is_dir():
        return r
    hits = [Path(x) for x in jobs_roots.indexed_roots() if Path(x) != r and (Path(x) / name).is_dir()]
    if len(hits) > 1:
        raise JobError(f"job {name} exists in several indexed roots ({', '.join(map(str, hits))}); pass --root")
    if hits:
        print(f"nexus-lite job: {name} found in the indexed root {hits[0]}", file=sys.stderr)
        return hits[0]
    return r


def log_path(name: str, *, root=None) -> Path:
    return _dir(name, root) / "log"


EXIT = {OK: 0, SKIP: 0, FAIL: 1, GONE: 4}  # anything else (RUNNING, AMBIGUOUS, UNKNOWN): 3; usage or refusal: 2
XRUN_EXIT = {OK: 0, SKIP: 1, FAIL: 1, GONE: 1}  # `wait --xrun-exit-codes`: only OK is 0; a timeout / not terminal stays 3


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
    s.add_argument("--notify", metavar="TARGET", default=None,
                   help="send one `job` bus message to TARGET at the terminal state (then `bus wait` is enough)")
    s.add_argument("--mem-max", metavar="SIZE", default=os.environ.get("NEXUS_LITE_MEM_MAX"),
                   help="opt-in memory cap: 64G, 512M or N%% of the machine/container memory (systemd scope when "
                        "available, else a watchdog); default none ($NEXUS_LITE_MEM_MAX); off/0 = none")
    s.add_argument("--own-run-dir", action="store_true",
                   help="export RUN_DIR=<this run's dir> even if the caller already has a RUN_DIR")
    st = sub.add_parser("status", help="one line per job")
    st.add_argument("name", nargs="?")
    w = sub.add_parser("wait", help="block until terminal; print ONE line; exit code from the state")
    w.add_argument("name")
    w.add_argument("--timeout", type=float)
    w.add_argument("--xrun-exit-codes", action="store_true",
                   help="xrun semantics: exit 0 only for OK, 1 for every other terminal state (3 on timeout)")
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
    ls = sub.add_parser("list", help="one line per job under the root")
    ls.add_argument("--all", action="store_true", help="every root in the roots index, one `== ROOT` section each")
    sub.add_parser("log", help="print the log path of a job").add_argument("name")
    rt = sub.add_parser("roots", help="list the indexed job roots (runs, non-terminal runs)")
    rt.add_argument("--prune", action="store_true", help="drop index entries whose directory is gone")
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
                      sentinel=a.sentinel, assert_cmd=a.assert_cmd, min_size=a.min_size, force=a.force, keep=a.keep,
                      notify=a.notify, mem_max=a.mem_max, own_run_dir=a.own_run_dir)
            print(f"{a.name} {r['state']} {r['dir']}" + (f" ({r['detail']})" if r.get("detail") else ""))
            return 3 if r["state"] in (UNKNOWN, AMBIGUOUS) else 0
        if a.verb == "list" and a.all:
            for r, rows in list_all():
                print(f"== {r}" if rows is not None else f"== {r} (MISSING: indexed root no longer exists)")
                for row in rows or []:
                    print(row.line())
            return 0
        if a.verb in ("status", "list"):
            rows = [status(a.name, root=locate(a.name, a.root))] if getattr(a, "name", None) \
                else list_jobs(root=a.root)
            for row in rows:
                print(row.line())
            return 0
        if a.verb == "wait":
            row = wait(a.name, root=locate(a.name, a.root), timeout=a.timeout)
            print(row.line())
            return (XRUN_EXIT if a.xrun_exit_codes else EXIT).get(row.state, 3)
        if a.verb == "check":
            print(json.dumps(check(a.name, root=locate(a.name, a.root))))
            return 0
        if a.verb == "log":
            print(log_path(a.name, root=locate(a.name, a.root)))
            return 0
        if a.verb == "roots":
            for r in (jobs_roots.prune() if a.prune else []):
                print(f"PRUNED {r}")
            for r in jobs_roots.indexed_roots():
                rows = list_jobs(root=r) if Path(r).is_dir() else None
                print(f"{r} (MISSING)" if rows is None else
                      f"{r} runs={len(rows)} non_terminal={sum(x.state not in TERMINAL for x in rows)}")
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
            print("\n".join(tail(a.name, a.n, root=locate(a.name, a.root))))
            return 0
        if a.verb == "stop":
            print(stop(a.name, root=locate(a.name, a.root)).line())
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
    if len(argv) == 2 and argv[0] == "_scoperun":
        return _scoperun(Path(argv[1]))
    print("internal entry point; use `nexus-lite job ...`", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
