"""
nexus_lite.agent_run — `nexus-lite agent run`: message -> command -> output -> message.

Contract: executes one command per input message. It passes the UTF-8 message body on stdin,
publishes stdout only after exit code 0, treats stderr as logs, acknowledges the message after
recording the outcome, and makes no exactly-once guarantee for command side effects.

The command is opaque: any argv (shell=False) or, only when asked, a shell string. It runs in a
new process group with an allowlisted env; timeout/stop terminates the whole group. Stdlib only;
edges: `._db` (workspace/lock dir), `.memory.mtm` (bus).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Iterator, List, Optional, Sequence, Union

from ._db import get_workspace
from .artifacts import mkdir_private
from .memory import mtm
from .protocol import validate_bus_field

CONSUMER = "agent"  # bus cursor used by `agent run`; independent of pty/watch/turn cursors
ON_FAILURE = ("notify-sender", "silent")
EXIT_BUSY = 75  # EX_TEMPFAIL: another runner holds this agent name
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
GRACE_S = 2.0
_TICK_S = 0.1

Command = Union[str, Sequence[str]]


class AgentBusy(RuntimeError):
    """Another `agent run` already holds this agent name (single-flight)."""


@dataclass
class RunResult:
    outcome: str  # "ok" | "failed" | "timeout" | "cancelled"
    exit_code: Optional[int]
    stdout: str

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


@contextlib.contextmanager
def agent_lock(name: str) -> Iterator[None]:
    """Hold the single-flight lock for agent `name` (flock on <ws>/agents/<name>.lock)."""
    validate_bus_field("name", name)
    d = get_workspace() / "agents"
    mkdir_private(d)
    fd = os.open(str(d / f"{name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentBusy(f"agent {name!r} is already running") from None
        yield
    finally:
        os.close(fd)


def agent_env(name: str, pass_env: Sequence[str] = (), base: Optional[dict] = None) -> dict:
    """Child env. Default: allowlisted vars + `pass_env` names from this process's env. With `base` (a consumer that
    builds each member's environment, e.g. a sandbox), exactly `base`. Always + NEXUS_AGENT + NEXUS_LITE_DIR."""
    if base is not None:
        env = dict(base)
    else:
        env = {k: os.environ[k] for k in (*ENV_ALLOWLIST, *pass_env) if k in os.environ}
    env["NEXUS_AGENT"] = name
    env["NEXUS_LITE_DIR"] = str(get_workspace())
    return env


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_group(proc: subprocess.Popen, grace_s: float) -> None:
    """SIGTERM the process group, wait up to `grace_s`, then SIGKILL whatever is left."""
    pgid = proc.pid  # start_new_session=True: the child leads its own group
    if not _group_alive(pgid):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    end = time.monotonic() + grace_s
    while time.monotonic() < end and _group_alive(pgid):
        proc.poll()  # reap the leader so it does not keep the group 'alive' as a zombie
        time.sleep(0.02)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def run_command(
    command: Command,
    body: str,
    *,
    env: Optional[dict] = None,
    shell: bool = False,
    timeout: Optional[float] = None,
    stop: Optional[threading.Event] = None,
    log: Optional[IO[str]] = None,
    grace_s: float = GRACE_S,
    cwd: Optional[str] = None,
) -> RunResult:
    """Run `command` once with `body` on stdin; stderr goes to `log` (default sys.stderr)."""
    if isinstance(command, str) and not shell:
        raise TypeError("a command string needs shell=True; pass an argv list otherwise")
    proc = subprocess.Popen(
        command, shell=shell, env=env, cwd=cwd, start_new_session=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = None if timeout is None else time.monotonic() + timeout
    data: Optional[bytes] = body.encode("utf-8")
    reason = None
    while True:
        try:
            out, err = proc.communicate(data, timeout=_TICK_S)
            break
        except subprocess.TimeoutExpired:
            data = None  # input is sent once; retries keep the output collected so far
            if stop is not None and stop.is_set():
                reason = "cancelled"
            elif deadline is not None and time.monotonic() >= deadline:
                reason = "timeout"
            elif proc.poll() is None:
                continue
            # cancelled, timed out, or the leader exited while a descendant holds the pipes
            _kill_group(proc, grace_s)
            out, err = proc.communicate()
            break
    _kill_group(proc, grace_s)  # no descendant outlives the invocation
    if err:
        (log or sys.stderr).write(err.decode("utf-8", errors="replace"))
        (log or sys.stderr).flush()
    rc = proc.returncode
    if reason is None:
        reason = "ok" if rc == 0 else "failed"
    return RunResult(reason, rc if reason in ("ok", "failed") else None,
                     out.decode("utf-8", errors="replace") if reason == "ok" else "")


invoke = run_command  # one call, no loop: `agent run` repeats this per message


CONTEXT_IN = "{context_in}"
CONTEXT_OUT = "{context_out}"


def expand_command(command: Command, context_in: str, context_out: str) -> Command:
    """Fill the context placeholders. argv: literal substitution per element; shell string: quoted.

    `context_in` is "" when there is no context (by convention an empty path means none).
    """
    if isinstance(command, str):
        return command.replace(CONTEXT_IN, shlex.quote(context_in)).replace(CONTEXT_OUT, shlex.quote(context_out))
    return [a.replace(CONTEXT_IN, context_in).replace(CONTEXT_OUT, context_out) for a in command]


def _uses_context(command: Command) -> bool:
    parts = [command] if isinstance(command, str) else list(command)
    return any(CONTEXT_IN in a or CONTEXT_OUT in a for a in parts)


def _record(name: str, msg: dict, result: RunResult, log: Optional[IO[str]]) -> None:
    (log or sys.stderr).write(
        f"[agent-run] {name} seq={msg['seq']} outcome={result.outcome} exit={result.exit_code}\n")


def _failure_text(result: RunResult) -> str:
    if result.outcome == "timeout":
        return "worker failed: timeout"
    return f"worker failed: exit={result.exit_code}"


class ContextRef:
    """The loop's current context reference (in memory only). `set` is an external change and bumps the
    generation; `advance` is the loop's own promotion and applies only if no external change came in."""

    def __init__(self, value: Optional[str] = None) -> None:
        self._value, self._gen = value or None, 0
        self._lock = threading.Lock()

    def get(self) -> tuple:
        with self._lock:
            return self._value, self._gen

    def set(self, value: Optional[str]) -> None:
        with self._lock:
            self._value, self._gen = value or None, self._gen + 1

    def advance(self, gen: int, fn: Callable[[Optional[str]], Optional[str]]) -> bool:
        with self._lock:
            if gen != self._gen:
                return False
            self._value = fn(self._value)
            return True


def _promote(current: Optional[str], out: Path, ctx_dir: Path, ok: bool) -> Optional[str]:
    """Next context reference after a call that was given `out` as its `{context_out}`."""
    if not (ok and out.exists()):
        out.unlink(missing_ok=True)
        return current
    if current and Path(current).parent == ctx_dir and Path(current) != out:
        Path(current).unlink(missing_ok=True)  # superseded output owned by this loop
    return str(out)


def _next_message(name: str) -> Optional[dict]:
    """Oldest unacked direct message for `name`; broadcasts and self-sent messages are acked, not run."""
    while True:
        msgs = mtm.bus_read(target=name, unacked=True, consumer=CONSUMER, limit=1)
        if not msgs:
            return None
        m = msgs[0]
        if m["target"] == name and m["sender"] != name:
            return m
        mtm.bus_ack(name, m["seq"], consumer=CONSUMER)


def agent_run(
    name: str,
    command: Command,
    *,
    shell: bool = False,
    on_failure: str = "notify-sender",
    timeout: Optional[float] = None,
    pass_env: Sequence[str] = (),
    poll_s: float = 0.5,
    max_messages: Optional[int] = None,
    exit_when_idle: bool = False,
    stop: Optional[threading.Event] = None,
    log: Optional[IO[str]] = None,
    context: Union[None, str, ContextRef] = None,
    wake: Optional[threading.Event] = None,
    on_status: Optional[Callable[..., None]] = None,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    _locked: bool = False,
) -> int:
    """Run `command` once per message addressed to `name`, FIFO by seq, single-flight per name.

    Returns the number of messages whose command ran to an outcome (ok/failed/timeout). Ends when
    `stop` is set (a running command is cancelled: no publish, no ack), after `max_messages`, or
    on the first idle poll with `exit_when_idle`.

    Context: `{context_in}` / `{context_out}` in the command are filled per call. The reference
    starts as `context` ("" = none); only a successful call that wrote its `{context_out}` file
    promotes it. Outputs the loop created are removed once failed or superseded; an external
    reference is never touched.
    """
    validate_bus_field("name", name)
    if on_failure not in ON_FAILURE:
        raise ValueError(f"on_failure must be one of {ON_FAILURE}")
    if isinstance(command, str) and not shell:
        raise TypeError("a command string needs shell=True; pass an argv list otherwise")
    stop = stop or threading.Event()
    wake = wake or stop  # an idle poll ends early on wake (or stop)
    status = on_status or (lambda _s, **_detail: None)
    child_env = agent_env(name, pass_env, base=env)
    ctx_dir = get_workspace() / "agents" / name / "context"
    ref = context if isinstance(context, ContextRef) else ContextRef(context)
    done = 0
    with (contextlib.nullcontext() if _locked else agent_lock(name)):
        while not stop.is_set() and (max_messages is None or done < max_messages):
            msg = _next_message(name)
            if msg is None:
                if exit_when_idle:
                    break
                status("idle")
                wake.wait(poll_s)
                if wake is not stop:
                    wake.clear()
                continue
            status("busy", seq=msg["seq"])
            out = None
            argv = command
            if _uses_context(command):
                out = mkdir_private(ctx_dir) / f"{msg['seq']}.ctx"
                current, gen = ref.get()
                argv = expand_command(command, current or "", str(out))
            result = run_command(argv, msg["content"] or "", env=child_env, shell=shell, cwd=cwd,
                                 timeout=timeout, stop=stop, log=log)
            if out is not None and not ref.advance(gen, lambda cur: _promote(cur, out, ctx_dir, result.ok)):
                if result.ok and out.exists():  # an external context set/reset came in during the call: it wins
                    (log or sys.stderr).write(f"[agent-run] {name} seq={msg['seq']} context_out kept, "
                                              f"not promoted (external context change): {out}\n")
                else:
                    out.unlink(missing_ok=True)
            if result.outcome == "cancelled":
                break  # left unacked: it may run again after restart
            _record(name, msg, result, log)
            if result.ok and result.stdout:
                mtm.bus_send(msg["sender"], result.stdout, sender=name)
            elif not result.ok and on_failure == "notify-sender":
                mtm.bus_send(msg["sender"], _failure_text(result), sender=name)
            mtm.bus_ack(name, msg["seq"], consumer=CONSUMER)
            done += 1
    return done


STATUSES = ("starting", "idle", "busy", "stopped")
CONTROL_OPS = ("status", "wake", "stop", "context-set", "context-reset")
_SUN_PATH_MAX = 103  # macOS sun_path is 104 bytes including the terminating NUL


class ControlUnavailable(RuntimeError):
    """No loop is listening on the control socket (nothing is queued or kept)."""


_NAME_BUDGET = 40  # bytes of agent name the default run dir must leave room for


def _run_dir_path() -> Path:
    """Workspace-scoped runtime dir: <ws>/run, or /tmp/nx-<12-hex workspace hash> when <ws>/run leaves too little
    room for a socket name (the same rule as the PTY socket dir; the hash only separates workspaces)."""
    ws = get_workspace()
    run = ws / "run"
    if len(str(run).encode()) + len("/agent-.ctl") + _NAME_BUDGET <= _SUN_PATH_MAX:
        return run
    return Path("/tmp") / ("nx-" + hashlib.sha256(str(ws).encode()).hexdigest()[:12])


def _run_dir() -> Path:
    d = _run_dir_path()
    if d.parent != Path("/tmp"):
        return mkdir_private(d)
    try:
        os.mkdir(str(d), 0o700)
    except FileExistsError:
        pass
    st = os.lstat(str(d))
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f"runtime dir {d} is not a directory owned by this user; refusing to use it")
    return d


def _checked(path: Path) -> Path:
    if len(str(path).encode()) > _SUN_PATH_MAX:
        raise ValueError(f"control socket path too long ({len(str(path).encode())} > {_SUN_PATH_MAX} bytes): "
                         f"{path}; use a shorter NEXUS_LITE_DIR")
    return path


def control_path(name: str) -> Path:
    """<run dir>/agent-NAME.ctl: <ws>/run, or /tmp/nx-<workspace hash> for a long workspace path (dir 0700)."""
    validate_bus_field("name", name)
    return _checked(_run_dir_path() / f"agent-{name}.ctl")


def control(name: str, op: str, *, path: Optional[str] = None, timeout: float = 2.0) -> dict:
    """Send one control request to the loop `name` and return its reply (volatile: nothing is stored).

    ops: status | wake | stop | context-set (path) | context-reset. Raises ControlUnavailable when no
    loop listens, ValueError for an unknown op or a refused request.
    """
    if op not in CONTROL_OPS:
        raise ValueError(f"op must be one of {CONTROL_OPS}")
    if (op == "context-set") != (path is not None):
        raise ValueError("context-set needs path; other ops take none")
    target = control_path(name)
    if not target.exists():
        raise ControlUnavailable(f"agent {name!r} is not running (no {target})")
    me = _checked(_run_dir() / f"c{os.getpid()}-{threading.get_ident() % 100000}.s")
    me.unlink(missing_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.bind(str(me))
        s.settimeout(timeout)
        try:
            s.sendto(json.dumps({"op": op, "path": path}).encode(), str(target))
            reply = json.loads(s.recv(65536).decode())
        except (ConnectionRefusedError, FileNotFoundError):
            raise ControlUnavailable(f"agent {name!r} is not running (nobody listens on {target})") from None
        except socket.timeout:
            raise ControlUnavailable(f"agent {name!r} did not answer within {timeout}s") from None
    finally:
        s.close()
        me.unlink(missing_ok=True)
    if not reply.get("ok"):
        raise ValueError(reply.get("error", "refused"))
    return reply


class AgentLoop:
    """`agent_run` in a thread, observed by its status (the only state).

    status(): starting | idle | busy | stopped. on_status_change(fn) calls fn(old, new) in the loop's
    thread on every change (never for the same value); it is a notification, not a separate state.
    """

    def __init__(self, name: str, command: Command, **run_kw) -> None:
        validate_bus_field("name", name)
        self._context = ContextRef(run_kw.pop("context", None))
        self.name, self.command, self._kw = name, command, run_kw
        self._status = "stopped"
        self._cond = threading.Condition()
        self._callbacks: List[Callable[[str, str], None]] = []
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._name_lock: Optional[contextlib.ExitStack] = None
        self._seq: Optional[int] = None
        self.error: Optional[BaseException] = None

    def status(self) -> str:
        with self._cond:
            return self._status

    def on_status_change(self, fn: Callable[[str, str], None]) -> None:
        self._callbacks.append(fn)

    def info(self) -> dict:
        """Lifecycle only: status, and the seq of the message being processed while busy.
        Results and history are execution records (the log), not status."""
        with self._cond:
            return {"status": self._status, "seq": self._seq}

    def _set_status(self, new: str, seq: Optional[int] = None) -> None:
        with self._cond:
            self._seq = seq if new == "busy" else None
            if new == self._status:
                return
            old, self._status = self._status, new
            self._cond.notify_all()
        for fn in list(self._callbacks):
            try:
                fn(old, new)
            except Exception as e:  # a consumer callback never breaks the loop
                self._log(f"[agent-run] {self.name} status callback error: {e!r}\n")

    def _log(self, text: str) -> None:
        out = self._kw.get("log") or sys.stderr
        out.write(text)
        out.flush()

    def start(self) -> "AgentLoop":
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(f"agent loop {self.name!r} is already started")
        stack = contextlib.ExitStack()
        stack.enter_context(agent_lock(self.name))  # AgentBusy here: the other loop's socket stays untouched
        try:
            path = control_path(self.name)
            _run_dir()
            path.unlink(missing_ok=True)  # stale: we hold the name lock, so no live loop owns it
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            stack.callback(sock.close)
            sock.bind(str(path))
            stack.callback(path.unlink, missing_ok=True)
            sock.settimeout(0.2)
        except BaseException:
            stack.close()
            raise
        self._sock, self._name_lock, self.error = sock, stack, None
        self._stop.clear()
        self._wake.clear()
        self._set_status("starting")
        self._server = threading.Thread(target=self._serve, name=f"agent-{self.name}-ctl", daemon=True)
        self._server.start()
        self._thread = threading.Thread(target=self._run, name=f"agent-{self.name}", daemon=True)
        self._thread.start()
        return self

    def _handle(self, req: dict) -> dict:
        op = req.get("op")
        if op == "status":
            return {"ok": True, **self.info()}
        if op == "wake":
            self.wake()
        elif op == "stop":
            self.request_stop()
        elif op == "context-set" and isinstance(req.get("path"), str) and req["path"]:
            self.set_context(req["path"])
        elif op == "context-reset":
            self.reset_context()
        else:
            return {"ok": False, "error": f"unknown or malformed request: {op!r}"}
        return {"ok": True, **self.info()}

    def _serve(self) -> None:
        sock = self._sock
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                reply = self._handle(json.loads(data.decode()))
            except ValueError:
                reply = {"ok": False, "error": "malformed request"}
            if addr:
                with contextlib.suppress(OSError):
                    sock.sendto(json.dumps(reply).encode(), addr)

    def _run(self) -> None:
        try:
            agent_run(self.name, self.command, stop=self._stop, wake=self._wake,
                      on_status=self._set_status, context=self._context, _locked=True, **self._kw)
        except Exception as e:
            self.error = e
            self._log(f"[agent-run] {self.name} loop ended: {e!r}\n")
        finally:
            self._stop.set()
            if self._server is not None:
                self._server.join()
            if self._name_lock is not None:
                self._name_lock.close()  # socket closed and removed, name lock released
            self._set_status("stopped")

    def wake(self) -> None:
        self._wake.set()

    def set_context(self, path: str) -> None:
        """Next invocation uses `path` as {context_in}; a call already running keeps its input."""
        self._context.set(path)

    def reset_context(self) -> None:
        """Next invocation runs without context ({context_in} is '')."""
        self._context.set(None)

    def context(self) -> Optional[str]:
        return self._context.get()[0]

    def request_stop(self) -> None:
        """Ask the loop to stop without waiting (safe from a signal handler or the control thread)."""
        self._stop.set()
        self._wake.set()

    def stop(self, timeout: Optional[float] = None) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout)

    def wait(self, until: str, timeout: Optional[float] = None) -> bool:
        if until not in STATUSES:
            raise ValueError(f"until must be one of {STATUSES}")
        with self._cond:
            return self._cond.wait_for(lambda: self._status == until, timeout)


USAGE = (
    "nexus-lite agent run NAME [--on-failure notify-sender|silent] [--timeout S] -- COMMAND [ARGS...]\n"
    "       nexus-lite agent run NAME [...] --shell 'COMMAND STRING'\n"
    "       nexus-lite agent ctl NAME wake | stop | context set PATH | context reset\n"
    "       nexus-lite agent status NAME\n"
    "       nexus-lite agent wait NAME --until starting|idle|busy|stopped [--timeout S]"
)
EXIT_WAIT_TIMEOUT = 1
EXIT_NOT_RUNNING = 3

DESCRIPTION = (
    "Run COMMAND once per bus message addressed to NAME (FIFO, one at a time). The UTF-8 message\n"
    "body is the command's stdin. Exit 0 + non-empty stdout is sent back to the message's sender\n"
    "(from NAME); empty stdout sends nothing; stderr is a log and never goes on the bus. On a\n"
    "nonzero exit or timeout nothing from stdout is published; --on-failure notify-sender (default)\n"
    "sends only 'worker failed: exit=<code>' or 'worker failed: timeout', silent sends nothing.\n"
    "The outcome is recorded, then the message is acked. No exactly-once guarantee: a message that\n"
    "was running when the runner stopped or crashed may run again. The command runs without a shell\n"
    "(argv after --) unless --shell is given, in a new process group, with an allowlisted env\n"
    f"({', '.join(ENV_ALLOWLIST)}, plus --env names, NEXUS_AGENT=NAME, NEXUS_LITE_DIR).\n"
    "SIGTERM/SIGINT/SIGHUP stop the runner and kill a running command's process group.\n"
    "Broadcast (ALL) and self-sent messages are acked without running the command.\n"
    "Context: {context_in} / {context_out} in COMMAND are filled per call ({context_in} is '' when there\n"
    "is none; start with --context PATH). Only a successful call that wrote {context_out} promotes it.\n"
    "ctl/status/wait talk to a running loop over <ws>/run/agent-NAME.ctl (volatile, nothing stored).\n"
    "status prints the lifecycle state: starting | idle | busy seq=N | stopped.\n"
    f"Exit codes: 0 ok, {EXIT_WAIT_TIMEOUT} wait timed out, 2 usage, {EXIT_NOT_RUNNING} NAME not running, "
    f"{EXIT_BUSY} another runner holds NAME."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nexus-lite agent run", usage=USAGE, description=DESCRIPTION,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="agent name on the bus (messages to NAME are its input)")
    p.add_argument("--on-failure", choices=ON_FAILURE, default="notify-sender")
    p.add_argument("--timeout", type=float, default=None, help="seconds per command (default: none)")
    p.add_argument("--shell", default=None, metavar="CMD", help="run CMD through /bin/sh instead of argv")
    p.add_argument("--env", action="append", default=[], metavar="VAR",
                   help="also pass env var VAR to the command (repeatable)")
    p.add_argument("--poll", type=float, default=0.5, metavar="S", help="idle poll interval (default: 0.5)")
    p.add_argument("--max-messages", type=int, default=None, metavar="N", help="exit after N messages")
    p.add_argument("--exit-when-idle", action="store_true", help="exit when no message is waiting")
    p.add_argument("--context", default=None, metavar="PATH",
                   help="initial context reference for {context_in} (default: none)")
    return p


def cli_main(argv: Sequence[str]) -> int:
    """`nexus-lite agent ...`; argv excludes the leading 'agent'."""
    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(f"usage: {USAGE}\n\n{DESCRIPTION}")
        return 0 if argv else 2
    if argv[0] in ("ctl", "status", "wait"):
        return _control_cli(argv[0], argv[1:])
    if argv[0] != "run":
        print(f"usage: {USAGE}", file=sys.stderr)
        return 2
    rest = argv[1:]
    cmd: list = []
    if "--" in rest:
        i = rest.index("--")
        rest, cmd = rest[:i], rest[i + 1:]
    parser = build_parser()
    a = parser.parse_args(rest)
    if bool(cmd) == (a.shell is not None):
        parser.error("give exactly one of: -- COMMAND [ARGS...]  or  --shell 'COMMAND STRING'")
    loop = AgentLoop(a.name, a.shell if a.shell is not None else cmd, shell=a.shell is not None,
                     on_failure=a.on_failure, timeout=a.timeout, pass_env=a.env, poll_s=a.poll,
                     max_messages=a.max_messages, exit_when_idle=a.exit_when_idle, context=a.context)
    # SIGHUP too: a runner inside a PTY gets it when the session is closed; it must not leave its command group behind
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: loop.request_stop())
    try:
        loop.start()
    except AgentBusy as e:
        print(f"nexus-lite agent run: {e}", file=sys.stderr)
        return EXIT_BUSY
    except (ValueError, OSError) as e:  # e.g. control socket path too long for this workspace
        print(f"nexus-lite agent run: {e}", file=sys.stderr)
        return 2
    while loop.status() != "stopped":
        loop.wait(until="stopped", timeout=0.2)  # short slices so signal handlers run promptly
    return 0


def _format_status(reply: dict) -> str:
    return f"busy seq={reply['seq']}" if reply["status"] == "busy" else reply["status"]


def _control_cli(cmd: str, argv: Sequence[str]) -> int:
    """`agent ctl|status|wait NAME ...`: the same status a library consumer reads, over the control socket."""
    p = argparse.ArgumentParser(prog=f"nexus-lite agent {cmd}", usage=USAGE)
    p.add_argument("name")
    if cmd == "ctl":
        p.add_argument("op", nargs="+", help="wake | stop | context set PATH | context reset")
    if cmd == "wait":
        p.add_argument("--until", required=True, choices=STATUSES)
        p.add_argument("--timeout", type=float, default=None, metavar="S")
    a = p.parse_args(list(argv))
    try:
        if cmd == "status":
            print(_format_status(control(a.name, "status")))
        elif cmd == "ctl":
            ops = {("wake",): ("wake", None), ("stop",): ("stop", None), ("context", "reset"): ("context-reset", None)}
            if len(a.op) == 3 and a.op[:2] == ["context", "set"]:
                op, path = "context-set", a.op[2]
            elif tuple(a.op) in ops:
                op, path = ops[tuple(a.op)]
            else:
                p.error(f"unknown control request: {' '.join(a.op)}")
            control(a.name, op, path=path)
        else:
            deadline = None if a.timeout is None else time.monotonic() + a.timeout
            while control(a.name, "status")["status"] != a.until:  # CLI convenience: same status, re-read
                if deadline is not None and time.monotonic() >= deadline:
                    return EXIT_WAIT_TIMEOUT
                time.sleep(0.05)
    except ControlUnavailable as e:
        if cmd == "wait" and a.until == "stopped":
            return 0  # no loop listening: it is stopped
        print(f"nexus-lite agent {cmd}: {e}", file=sys.stderr)
        return EXIT_NOT_RUNNING
    return 0
