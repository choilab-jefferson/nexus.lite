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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

# TODO: the Go module path / release URL for loom is pending an owner decision
# Do not hardcode one; set LOOM_INSTALL_URL meanwhile.
LOOM_INSTALL_URL = os.environ.get("LOOM_INSTALL_URL")

DEFAULT_EXIT_CODES: Dict[str, int] = {
    "ok": 0,
    "task_failure": 1,
    "harness_or_policy_error": 2,
    "escalation_required": 3,
    "budget": 4,
    "interrupted": 130,
}


def find_loom() -> Optional[str]:
    """Return the loom binary path (LOOM_BIN, else PATH lookup) or None."""
    return os.environ.get("LOOM_BIN") or shutil.which("loom")


@dataclass
class LoomProbe:
    available: bool
    path: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    error: str = ""


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
    return LoomProbe(True, path, caps, "")


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
    def __init__(self, bin: Optional[str] = None, timeout: Optional[float] = None):
        self.bin = bin
        self.timeout = timeout
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
                timeout=self.timeout, env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return LoomResult(None, "timeout", stdout=out, stderr=err, trace_path=trace_path,
                              trace=_read_trace(trace_path))
        except OSError as e:
            return LoomResult(None, "unavailable", stderr=str(e), trace_path=trace_path)
        status = self._status(p.returncode)
        try:
            parsed = json.loads(p.stdout) if p.stdout.strip() else None
        except ValueError:
            parsed = None
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
