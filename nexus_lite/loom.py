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
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# Pinned released loom binary. Nothing here downloads anything:
# install_loom() is the only network path and runs only when called explicitly.
LOOM_VERSION = "0.3.2"
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
