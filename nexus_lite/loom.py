"""Minimal subprocess adapter for the `loom` agent harness.

Stdlib only. When loom is not installed every call degrades gracefully to a
LoomResult with status "unavailable" instead of raising.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import hashlib
import platform
import sys
import time
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# Pinned released loom binary. Nothing here downloads anything:
# install_loom() is the only network path and runs only when called explicitly.
LOOM_VERSION = "0.4.0"
LOOM_RELEASE_BASE = "https://github.com/choilab-jefferson/nexus.loom/releases/download/v{version}"
# Optional env override of the release base URL (directory holding the assets).
LOOM_INSTALL_URL = os.environ.get("LOOM_INSTALL_URL")

MIN_LOOM_VERSION = "0.1.0"

_ENV_ALLOW = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY", "OLLAMA_HOST", "OPENAI_BASE_URL",
})


def loom_env(extra: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Copy only allowlisted vars (plus LOOM_* and `extra` names) from os.environ."""
    allow = _ENV_ALLOW | set(extra or ())
    return {k: v for k, v in os.environ.items() if k in allow or k.startswith("LOOM_")}


def _parse_version(v: str) -> Optional[Tuple[int, ...]]:
    v = v.strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = v.split(".")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


DEFAULT_EXIT_CODES: Dict[str, int] = {
    "ok": 0,
    "task_failure": 1,
    "harness_or_policy_error": 2,
    "escalation_required": 3,
    "budget": 4,
    "interrupted": 130,
}


def _asset_name(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """Release asset name for this platform: loom-{linux,darwin}-{amd64,arm64}."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in ("linux", "darwin") or arch is None:
        raise RuntimeError(
            f"no released loom binary for {system}/{machine}; "
            "supported: linux and darwin on amd64/arm64 (build from source otherwise)")
    return f"loom-{system}-{arch}"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        try:
            return resp.read()
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()


def install_loom(version: str = LOOM_VERSION, dest: Optional[Union[str, Path]] = None,
                 base_url: Optional[str] = None) -> Path:
    """Explicitly download the released loom binary, verify sha256, install it.

    Never called implicitly (no import-time or LoomAdapter side effect). The
    base URL is `base_url`, else $LOOM_INSTALL_URL, else the GitHub release for
    `version`. `dest` defaults to ~/.local/bin/loom. Returns the installed path.
    """
    asset = _asset_name()
    base = (base_url or os.environ.get("LOOM_INSTALL_URL")
            or LOOM_RELEASE_BASE.format(version=version)).rstrip("/")
    sums = _fetch(f"{base}/checksums.txt").decode("utf-8", "replace")
    expected = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset:
            expected = parts[0].lower()
    if expected is None:
        raise RuntimeError(f"{asset} not listed in checksums.txt at {base}")
    data = _fetch(f"{base}/{asset}")
    actual = hashlib.sha256(data).hexdigest()
    target = Path(dest).expanduser() if dest else Path.home() / ".local" / "bin" / "loom"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    if actual != expected:
        tmp.unlink()
        raise RuntimeError(f"sha256 mismatch for {asset}: expected {expected}, got {actual}")
    tmp.chmod(0o755)
    os.replace(tmp, target)
    return target


def find_loom() -> Optional[str]:
    """Return the loom binary path (LOOM_BIN, else PATH lookup) or None."""
    return os.environ.get("LOOM_BIN") or shutil.which("loom")


@dataclass
class LoomProbe:
    available: bool
    path: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    error: str = ""
    version: Optional[str] = None

    @property
    def version_ok(self) -> bool:
        """True when version is unknown/unparseable (permissive) or >= MIN_LOOM_VERSION."""
        parsed = _parse_version(self.version) if self.version else None
        return parsed is None or parsed >= _parse_version(MIN_LOOM_VERSION)


def probe(bin: Optional[str] = None, timeout: float = 10.0) -> LoomProbe:
    """Detect loom and read `loom capabilities --json`. Never raises."""
    path = bin or find_loom()
    if not path:
        return LoomProbe(False, None, None, "loom not found")
    try:
        p = subprocess.run(
            [path, "capabilities", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return LoomProbe(False, path, None, "capabilities probe timed out")
    except Exception as e:  # missing/non-executable binary, etc.
        return LoomProbe(False, path, None, f"{type(e).__name__}: {e}")
    if p.returncode != 0:
        return LoomProbe(False, path, None, f"capabilities exited {p.returncode}: {p.stderr.strip()}")
    try:
        caps = json.loads(p.stdout)
    except ValueError as e:
        return LoomProbe(False, path, None, f"invalid capabilities JSON: {e}")
    if not isinstance(caps, dict):
        return LoomProbe(False, path, None, "invalid capabilities JSON: not an object")
    ver = caps.get("version")
    pr = LoomProbe(True, path, caps, "", ver if isinstance(ver, str) else None)
    if not pr.version_ok:
        warnings.warn(f"loom {pr.version} is older than minimum {MIN_LOOM_VERSION}", stacklevel=2)
    return pr


@dataclass
class LoomResult:
    returncode: Optional[int]
    status: str
    ok: bool = False
    retryable: bool = False
    result: Optional[Any] = None
    stdout: str = ""
    stderr: str = ""
    trace_path: Optional[str] = None
    trace: List[Dict[str, Any]] = field(default_factory=list)


def _read_trace(path: Optional[str]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return events
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


class LoomAdapter:
    def __init__(self, bin: Optional[str] = None, timeout: Optional[float] = None,
                 env_passthrough: Optional[Iterable[str]] = None):
        self.bin = bin
        self.timeout = timeout
        self.env_passthrough = list(env_passthrough or [])
        self._codes: Optional[Dict[str, int]] = None

    def _path(self) -> Optional[str]:
        return self.bin or find_loom()

    def exit_codes(self) -> Dict[str, int]:
        """Built-in table, overlaid with `capabilities.exit_codes` when present."""
        if self._codes is None:
            codes = dict(DEFAULT_EXIT_CODES)
            caps = probe(self._path()).capabilities or {}
            extra = caps.get("exit_codes")
            if isinstance(extra, dict):
                codes.update({k: v for k, v in extra.items() if isinstance(v, int)})
            self._codes = codes
        return self._codes

    def _status(self, rc: int) -> str:
        for name, code in self.exit_codes().items():
            if code == rc:
                return name
        return "unknown"

    def _exec(self, args: List[str], trace_path: Optional[str]) -> LoomResult:
        path = self._path()
        if not path:
            return LoomResult(None, "unavailable", trace_path=trace_path)
        try:
            p = subprocess.run(
                [path, *args], capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=self.timeout, env=loom_env(self.env_passthrough),
            )
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return LoomResult(None, "timeout", stdout=out, stderr=err, trace_path=trace_path,
                              trace=_read_trace(trace_path))
        except OSError as e:
            return LoomResult(None, "unavailable", stderr=str(e), trace_path=trace_path)
        # rc<0: child killed by a signal (crash); not in loom's exit-code table.
        status = "crash" if p.returncode < 0 else self._status(p.returncode)
        try:
            parsed = json.loads(p.stdout) if p.stdout.strip() else None
        except ValueError:
            parsed = None
            if status == "ok" and p.stdout.strip():
                status = "bad_output"  # rc=0 but stdout is not the JSON result
        return LoomResult(
            returncode=p.returncode, status=status, ok=status == "ok",
            retryable=status == "task_failure", result=parsed,
            stdout=p.stdout, stderr=p.stderr, trace_path=trace_path,
            trace=_read_trace(trace_path),
        )

    @staticmethod
    def _flags(provider, model, workspace, max_turns, trace_path, extra_args) -> List[str]:
        args = ["--json"]
        if trace_path:
            args += ["--trace", str(trace_path)]
        for flag, val in (("--provider", provider), ("--model", model),
                          ("--workspace", workspace), ("--max-turns", max_turns)):
            if val is not None:
                args += [flag, str(val)]
        return args + list(extra_args or [])

    def run_task(self, task: Union[dict, str, "os.PathLike[str]"], provider=None, model=None,
                 workspace=None, max_turns=None, trace_path=None, extra_args=None) -> LoomResult:
        """Run `loom run --task-file`; a dict is written to a temp task.json."""
        flags = self._flags(provider, model, workspace, max_turns, trace_path, extra_args)
        if isinstance(task, dict):
            with tempfile.TemporaryDirectory() as d:
                tf = os.path.join(d, "task.json")
                with open(tf, "w", encoding="utf-8") as f:
                    json.dump(task, f)
                return self._exec(["run", "--task-file", tf, *flags], trace_path)
        return self._exec(["run", "--task-file", str(task), *flags], trace_path)

    def run_prompt(self, instruction: str, provider=None, model=None, workspace=None,
                   max_turns=None, trace_path=None, extra_args=None) -> LoomResult:
        """Run `loom -p <instruction>`."""
        flags = self._flags(provider, model, workspace, max_turns, trace_path, extra_args)
        return self._exec(["-p", instruction, *flags], trace_path)

    def serve(self, bus_dir: Union[str, "os.PathLike[str]"], name: Optional[str] = None, **opts) -> "LoomLoop":
        """Loop mode: a `LoomLoop` for `loom serve --bus-dir BUS_DIR --name NAME` (not started; use `with` or
        `.start()`). `opts`: host, workspace, provider, model, max_turns (user turns), max_wall, idle_timeout
        (seconds or Go durations), on_job_exit, poll_interval, extra_args, startup_timeout, rescan."""
        return LoomLoop(self, bus_dir, name=name, **opts)


class LoomLoopError(RuntimeError):
    """Loop mode cannot start or has failed (loom too old, bad bus, serve exited at startup)."""


@dataclass
class LoomTurn:
    """Outcome of one loop-mode request (`task`, `resume`).

    `status` follows the loom contract names plus the adapter's own "timeout" (no reply within `timeout`)
    and "exited" (serve ended first, `exit_code` is its exit code). `data` is the raw `loom.result` data.
    """
    status: str
    exit_code: Optional[int]
    ok: bool = False
    body: str = ""
    trace_path: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    message_id: Optional[str] = None


@dataclass
class LoomExtract:
    """A `loom.extract` reply: the blob path (absolute), its schema and the parsed JSON (sha256 verified)."""
    path: Path
    schema: str
    sha256: str
    obj: Any


def _dur(v: Union[int, float, str, None]) -> Optional[str]:
    """A Go duration flag value: numbers are seconds."""
    if v is None:
        return None
    return f"{v}s" if isinstance(v, (int, float)) else str(v)


class LoomLoop:
    """Drive one long-lived `loom serve` through the nexus file bus (docs/specs/file-bus.md section 13).

    Use as a context manager (or call `start()` / `stop()`). Requests are bus messages to `msg/<name>/`; replies
    land in `msg/<host>/` and are matched by `meta.corr`. Waits block on the bus wake-up path (loom sends a
    datagram to this process's waiter socket after each reply) with a rescan every `rescan` seconds as the
    fallback; there are no fixed sleeps. loom never opens SQLite and neither does this class: it uses the file
    bus only (`nexus_lite.filebus`).
    """

    CONSUMER = "loomloop"  # own cursor on the host inbox: other readers of that inbox are not disturbed

    def __init__(self, adapter: "LoomAdapter", bus_dir: Union[str, "os.PathLike[str]"], *,
                 name: Optional[str] = None, host: Optional[str] = None, workspace=None, provider=None,
                 model=None, max_turns=None, max_wall=None, idle_timeout=None, on_job_exit=None,
                 poll_interval=None, extra_args=None, startup_timeout: float = 15.0, rescan: float = 1.0):
        self.adapter = adapter
        self.bus_dir = Path(bus_dir).expanduser().resolve()
        self.name = name or f"loom-{os.getpid()}"
        self.host = host or f"{self.name}-host"
        self.startup_timeout = startup_timeout
        self.rescan = rescan
        flags: List[str] = []
        for flag, val in (("--workspace", workspace), ("--provider", provider), ("--model", model),
                          ("--max-turns", max_turns), ("--max-wall", _dur(max_wall)),
                          ("--idle-timeout", _dur(idle_timeout)), ("--on-job-exit", on_job_exit),
                          ("--poll-interval", _dur(poll_interval))):
            if val is not None:
                flags += [flag, str(val)]
        self._flags = flags + list(extra_args or [])
        self._cwd = str(workspace) if workspace is not None else None
        self._proc: Optional[subprocess.Popen] = None
        self._waiter = None
        self._bus = None
        self._logdir: Optional[tempfile.TemporaryDirectory] = None
        self._replies: Dict[str, Dict[str, Any]] = {}
        self.states: List[Dict[str, Any]] = []   # loom.state data, in arrival order
        self.jobs: List[Dict[str, Any]] = []     # job event data, in arrival order

    # --- lifecycle
    def __enter__(self) -> "LoomLoop":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.poll() if self._proc is not None else None

    @property
    def unclaimed(self) -> List[Dict[str, Any]]:
        """Replies nobody asked for (an `interrupted` result after a crash, a failed `inject`)."""
        return list(self._replies.values())

    def _serve_log(self) -> str:
        if self._logdir is None:
            return ""
        try:
            return Path(self._logdir.name, "serve.log").read_text(errors="replace").strip()[-2000:]
        except OSError:
            return ""

    def start(self) -> "LoomLoop":
        """Launch `loom serve` and wait until its session is live. Raises LoomLoopError if loop mode is missing
        (`capabilities --json` has no `loop_mode`), serve exits at startup, or the session never appears."""
        if self.running:
            return self
        from . import filebus
        path = self.adapter._path()
        pr = probe(path)
        if not pr.available:
            raise LoomLoopError(f"loom unavailable: {pr.error}")
        lm = (pr.capabilities or {}).get("loop_mode")
        if not isinstance(lm, dict) or lm.get("bus_format") != filebus.FORMAT or lm.get("bus_version") != filebus.VERSION:
            raise LoomLoopError(
                f"loom {pr.version or '?'} at {path} has no compatible loop mode (`loom capabilities --json` must "
                f"report loop_mode with bus_format {filebus.FORMAT} v{filebus.VERSION}); build or install a loom "
                "with `loom serve`")
        self._bus = filebus.open_bus(self.bus_dir)  # also creates the layout and VERSION before loom does
        self._waiter = filebus.Waiter(self.bus_dir, self.host)  # bound before the first scan
        self._logdir = tempfile.TemporaryDirectory(prefix="loomloop-")
        log = open(os.path.join(self._logdir.name, "serve.log"), "wb")
        try:
            self._proc = subprocess.Popen(
                [path, "serve", "--bus-dir", str(self.bus_dir), "--name", self.name, *self._flags],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=self._cwd,
                env=loom_env(self.adapter.env_passthrough), start_new_session=True)
        except OSError as e:
            self._cleanup()
            raise LoomLoopError(f"cannot launch loom serve: {e}") from e
        finally:
            log.close()
        # Session registration has no wake-up: this one startup wait is a short rescan loop.
        deadline = time.monotonic() + self.startup_timeout
        while True:
            rc = self._proc.poll()
            sess = self._bus.read_session(self.name) or {}
            if sess.get("live") and (sess.get("agent") or {}).get("kind") == "loom":
                return self
            if rc is not None:
                err = self._serve_log()
                self._cleanup()
                raise LoomLoopError(f"loom serve exited {rc} at startup: {err}")
            if time.monotonic() >= deadline:
                self._terminate()
                err = self._serve_log()
                self._cleanup()
                raise LoomLoopError(f"loom serve did not register session {self.name!r} within "
                                    f"{self.startup_timeout}s: {err}")
            self._waiter.wait(0.05)

    def _terminate(self, grace: float = 5.0) -> Optional[int]:
        p = self._proc
        if p is None:
            return None
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(grace)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        return p.returncode

    def _cleanup(self) -> None:
        if self._waiter is not None:
            self._waiter.close()
            self._waiter = None
        if self._logdir is not None:
            self._logdir.cleanup()
            self._logdir = None
        self._proc = None

    def close(self) -> Optional[int]:
        """Stop serve (SIGTERM, then SIGKILL) if still running and release local resources; returns its exit code."""
        rc = self._terminate()
        self._cleanup()
        return rc

    def stop(self, reason: Optional[str] = None, timeout: float = 10.0) -> Optional[int]:
        """Send `loom.stop`, wait up to `timeout` for serve to exit (loom handles it at the next turn boundary),
        else terminate it. Returns the exit code (0 after a clean stop). Unhandled later messages stay unacked,
        so `start()` again on the same bus dir and name continues."""
        if not self.running:
            return self.close()
        self._send("loom.stop", {"reason": reason} if reason else {})
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            pass
        self._drain()
        return self.close()

    # --- bus io
    def _send(self, kind: str, data: Dict[str, Any], body: str = "") -> str:
        if self._bus is None:
            raise LoomLoopError("loop not started")
        rec = self._bus.send(self.name, sender=self.host, kind=kind, summary=kind, body=body, data=data)
        self._bus.notify(self.name, rec["id"])
        return rec["id"]

    def _drain(self) -> None:
        bus = self._bus
        seen = []
        for mid in bus.unacked_ids(self.host, self.CONSUMER):
            rec = bus.read_message(self.host, mid)
            if rec is None:  # partial file: the next pass retries it
                continue
            seen.append(mid)
            data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            rec = dict(rec, data=data, body=bus.message_body(rec, self.host))
            kind = rec.get("kind")
            if kind == "loom.state":
                self.states.append(data)
            elif kind == "job":
                self.jobs.append(data)
            elif kind in ("loom.result", "loom.extracted"):
                corr = (rec.get("meta") or {}).get("corr") or data.get("request")
                if corr:
                    self._replies[corr] = rec
        if seen:
            bus.ack(self.host, self.CONSUMER, seen)

    def poll(self, timeout: float = 0.0) -> None:
        """Drain the host inbox into `states`, `jobs` and `unclaimed`; with `timeout` > 0 and nothing new yet,
        sleep on the wake-up path for up to that long first (async `job` and `loom.state` events)."""
        if self._bus is None:
            raise LoomLoopError("loop not started")
        n = (len(self.states), len(self.jobs), len(self._replies))
        self._drain()
        if timeout > 0 and n == (len(self.states), len(self.jobs), len(self._replies)):
            self._waiter.wait(timeout)
            self._drain()

    def _await(self, req_id: str, timeout: Optional[float]):
        """The reply to `req_id`; ("timeout", None) or ("exited", rc) when there will be none."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            rc = self._proc.poll() if self._proc is not None else 0
            self._drain()  # after the poll: everything serve wrote before exiting is visible
            rec = self._replies.pop(req_id, None)
            if rec is not None:
                return rec
            if rc is not None:
                return "exited", rc
            left = self.rescan if deadline is None else min(self.rescan, deadline - time.monotonic())
            if left <= 0:
                return "timeout", None
            self._waiter.wait(left)

    def _turn(self, req_id: str, timeout: Optional[float]) -> LoomTurn:
        rec = self._await(req_id, timeout)
        if isinstance(rec, tuple):
            what, rc = rec
            if what == "timeout":
                return LoomTurn("timeout", None, message_id=req_id)
            reason = next((s.get("reason") for s in reversed(self.states) if s.get("reason")), None)
            return LoomTurn(self.adapter._status(rc) if rc else "exited", rc,
                            data={"reason": reason} if reason else {}, message_id=req_id)
        d = rec["data"]
        code = d.get("exit_code")
        # Same names as LoomResult.status (ok, task_failure, budget, ...); loom's own word stays in data["status"].
        status = self.adapter._status(code) if isinstance(code, int) else str(d.get("status") or "unknown")
        trace = d.get("trace")
        if isinstance(trace, str) and not os.path.isabs(trace):
            trace = str(self.bus_dir / trace)
        return LoomTurn(status, code if isinstance(code, int) else None, ok=code == 0, body=rec["body"],
                        trace_path=trace, data=d, message_id=req_id)

    # --- requests
    def task(self, prompt: str, constraints: Optional[Dict[str, Any]] = None, *, context_files=None,
             timeout: Optional[float] = None) -> LoomTurn:
        """One user turn on the persistent context (`loom.task`); blocks until its `loom.result`."""
        data: Dict[str, Any] = {"prompt": prompt}
        if constraints:
            data["constraints"] = dict(constraints)
        if context_files:
            data["context_files"] = [str(f) for f in context_files]
        return self._turn(self._send("loom.task", data), timeout)

    def inject(self, text: Optional[str] = None, *, files=None, messages=None, role: str = "user") -> str:
        """Append `text` / `messages` / workspace-relative `files` to the context before the next turn
        (`loom.inject`). No reply is sent; returns the message id. A failed inject shows up in `unclaimed`."""
        msgs = [dict(m) for m in (messages or [])]
        if text is not None:
            msgs.append({"role": role, "content": text})
        data: Dict[str, Any] = {}
        if msgs:
            data["messages"] = msgs
        if files:
            data["files"] = [str(f) for f in files]
        if not data:
            raise ValueError("inject needs text, messages or files")
        return self._send("loom.inject", data)

    def extract(self, what: str = "context", *, timeout: Optional[float] = 30.0) -> LoomExtract:
        """Ask for the persistent context or a summary (`loom.extract`) and read the blob it points to."""
        if what not in ("context", "summary"):
            raise ValueError("what must be 'context' or 'summary'")
        rid = self._send("loom.extract", {"what": what})
        rec = self._await(rid, timeout)
        if isinstance(rec, tuple):
            raise LoomLoopError(f"no loom.extracted for {what}: {rec[0]}" + (f" (serve exit {rec[1]})" if rec[1] is not None else ""))
        d = rec["data"]
        if rec.get("kind") != "loom.extracted":
            raise LoomLoopError(f"extract {what} failed: {d.get('status')} {d.get('reason', '')}".strip())
        rel = d.get("path")
        blob = (self.bus_dir / rel).resolve() if isinstance(rel, str) else None
        if blob is None or self.bus_dir not in blob.parents:
            raise LoomLoopError(f"loom.extracted path outside the bus dir: {rel!r}")
        raw = blob.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != d.get("sha256"):
            raise LoomLoopError(f"sha256 mismatch for {rel}")
        return LoomExtract(blob, str(d.get("schema")), digest, json.loads(raw))

    def resume(self, checkpoint: Optional[str] = None, *, timeout: Optional[float] = None) -> LoomTurn:
        """Resume the last interrupted/paused turn (`loom.resume`; default = the last turn, else a trace path or
        its `.ckpt`) and wait for its `loom.result`."""
        return self._turn(self._send("loom.resume", {"checkpoint": str(checkpoint)} if checkpoint else {}), timeout)


def _max_steps_default() -> int:
    try:
        return max(1, int(os.environ.get("NEXUS_MAX_STEPS", "10")))
    except ValueError:
        return 10


@dataclass
class StepRecord:
    step: int
    status: str
    verified: bool
    goal: str
    output: str = ""


class LoomStateLoop:
    """Bounded iterate-until-verified loop around LoomAdapter.

    Each step writes an append-only context file for `loom --context-in`
    (blocks 1-3 never change between steps, so prefix caches stay hot), runs
    loom, reads `--context-out`, calls `verify(result) -> bool`, and checkpoints
    step metadata to lite.db. Stops on verify success, a non-retryable status,
    or `max_steps` (default NEXUS_MAX_STEPS, else 10).
    """

    def __init__(self, adapter: Optional[LoomAdapter] = None, *, run_id: str,
                 system: str = "", pack: str = "", max_steps: Optional[int] = None,
                 max_context_chars: int = 24000, checkpoint: bool = True):
        self.adapter = adapter or LoomAdapter()
        self.run_id = run_id
        self.system = system
        self.pack = pack
        self.max_steps = max_steps if max_steps is not None else _max_steps_default()
        self.max_context_chars = max_context_chars
        self.checkpoint = checkpoint
        self.ledger: List[str] = []
        self.records: List[StepRecord] = []

    def render_context(self, goal: str, verifier_output: str = "") -> str:
        """Frozen system, frozen pack, append-only ledger, then current goal."""
        head = f"[Frozen Role / System Spec]\n{self.system}\n\n[Frozen IKB Context Pack]\n{self.pack}\n\n"
        tail = f"\n[Current Step Goal & Verifier Output]\n{goal}\n{verifier_output}\n"
        ledger = list(self.ledger)
        budget = self.max_context_chars - len(head) - len(tail)
        # Bound size by dropping the oldest entries; the surviving prefix stays stable
        # once the ledger is at the cap (older steps are elided, never rewritten).
        while ledger and sum(len(e) + 1 for e in ledger) > budget:
            ledger.pop(0)
        return head + "[Append-Only Step Ledger]\n" + "\n".join(ledger) + tail

    def _save(self, rec: StepRecord) -> None:
        from . import _db
        with _db.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS loom_state_steps ("
                        "run_id TEXT, step INTEGER, status TEXT, verified INTEGER, goal TEXT, "
                        "ts REAL DEFAULT (strftime('%s','now')), PRIMARY KEY (run_id, step))")
            con.execute("INSERT OR REPLACE INTO loom_state_steps (run_id, step, status, verified, goal) "
                        "VALUES (?,?,?,?,?)", (self.run_id, rec.step, rec.status, int(rec.verified), rec.goal))

    def run(self, goal: str, verify, **run_kwargs) -> List[StepRecord]:
        verifier_output = ""
        extra = list(run_kwargs.pop("extra_args", None) or [])
        with tempfile.TemporaryDirectory() as d:
            for n in range(1, self.max_steps + 1):
                cin, cout = os.path.join(d, "ctx-in.txt"), os.path.join(d, "ctx-out.txt")
                if os.path.exists(cout):
                    os.remove(cout)  # never reuse a previous step's output
                Path(cin).write_text(self.render_context(goal, verifier_output), encoding="utf-8")
                res = self.adapter.run_prompt(
                    goal, extra_args=extra + ["--context-in", cin, "--context-out", cout], **run_kwargs)
                out = Path(cout).read_text(encoding="utf-8", errors="replace") if os.path.exists(cout) else ""
                verified = bool(res.ok and verify(res))
                rec = StepRecord(n, res.status, verified, goal, out)
                self.records.append(rec)
                self.ledger.append(f"step {n}: status={res.status} verified={verified} out={out.strip()[:500]}")
                if self.checkpoint:
                    self._save(rec)
                if verified or not (res.ok or res.retryable):
                    break
                verifier_output = f"previous step {n} not verified (status={res.status})"
        return self.records
