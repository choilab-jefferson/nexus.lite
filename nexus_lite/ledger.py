"""Step ledger: one append-only row per step event, so a multi-step driver survives its own death.

Row schema (``#nexus.ledger/2``), tab-separated:
``utc  step  status(START|OK|FAIL|SKIP)  seconds  logpath  sentinel  assert(PASS|FAIL|NONE)``.
One START row and one terminal row (OK|FAIL) per execution; a skipped step writes one SKIP row.

Skip rule: a step is skipped only if its sentinel exists AND its assertion passes NOW; an unasserted sentinel
re-runs. The sentinel is the step's real output, written beside itself and renamed into place (``commit``), so a
killed step never leaves a complete-looking sentinel. Recovery: the ledger is the history, the validated sentinel
is the truth; a START row without a terminal row is an interrupted execution. A torn last row (no newline) is
terminated before the next append; readers drop rows with the wrong column count.
"""
from __future__ import annotations

import calendar
import fcntl
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

HEADER = "#nexus.ledger/2"
COLUMNS = ("utc", "step", "status", "seconds", "logpath", "sentinel", "assert")

PathLike = Union[str, os.PathLike]


class LedgerError(Exception):
    pass


def _field(v) -> str:
    s = str(v if v is not None else "").replace("\t", " ").replace("\n", " ")
    return s or "-"


def append(ledger: PathLike, step: str, status: str, seconds, logpath, sentinel, verdict: str) -> None:
    """Append one row; atomic under concurrent appenders (one exclusive lock covers header, repair and row)."""
    path = Path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = "\t".join(_field(v) for v in (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), step, status,
                                       seconds, logpath, sentinel, verdict))
    with open(path, "a+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0, os.SEEK_END)
            out = b""
            if f.tell() == 0:
                out += (HEADER + "\n" + "#" + "\t".join(COLUMNS) + "\n").encode()
            else:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    out += b"\n"  # torn last row: terminate it, never merge into it
            f.write(out + (row + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def rows(ledger: PathLike) -> List[Dict[str, str]]:
    """Rows with exactly seven columns; comments and torn rows are dropped."""
    try:
        lines = Path(ledger).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        if ln.startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) == len(COLUMNS):
            out.append(dict(zip(COLUMNS, parts)))
    return out


def commit(tmp: PathLike, sentinel: PathLike) -> None:
    """Atomic output: fsync `tmp`, then rename it onto `sentinel`. `tmp` must be in the sentinel's own directory (a
    same-directory rename never crosses a mount)."""
    t, s = Path(tmp), Path(sentinel)
    if t.resolve().parent != s.resolve().parent:
        raise LedgerError(f"{t} is not in {s.parent}: write the temporary file beside the sentinel")
    with open(t, "rb") as f:
        os.fsync(f.fileno())
    os.replace(t, s)
    dfd = os.open(s.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ---------------------------------------------------------------- begin / mark / step
def _meta(sentinel, assert_cmd: Optional[str], min_size: Optional[int]) -> Dict:
    if min_size is not None and assert_cmd:
        raise LedgerError("use either min_size or assert_cmd, not both")
    if min_size is not None and (isinstance(min_size, bool) or not isinstance(min_size, int) or min_size < 1):
        raise LedgerError(f"min_size needs a positive byte count, got {min_size!r}")
    return {"sentinel": str(Path(sentinel).resolve()) if sentinel else None, "assert": assert_cmd,
            "min_size": min_size}


def _valid_now(meta: Dict, rc: Optional[int] = None, log=None) -> Optional[bool]:
    """True/False when an assertion is declared (the sentinel passes it now or not); None when unasserted."""
    from .jobs import _assertion, _asserted
    if not _asserted(meta):
        return None
    return _assertion(meta, rc, Path(log) if log else None) is None


def _since_start(ledger: PathLike, step: str) -> int:
    starts = [r["utc"] for r in rows(ledger) if r["step"] == step and r["status"] == "START"]
    if not starts:
        return 0
    try:
        t = calendar.timegm(time.strptime(starts[-1], "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return 0
    return max(0, int(time.time() - t))


def begin(ledger: PathLike, step: str, *, sentinel=None, assert_cmd: Optional[str] = None,
          min_size: Optional[int] = None, log=None, no_skip: bool = False) -> str:
    """Skip decision for a step: "SKIP" (SKIP row; the sentinel exists and passes its assertion now) or "RUN"
    (START row; do the step, then `mark`)."""
    meta = _meta(sentinel, assert_cmd, min_size)
    if not no_skip and meta["sentinel"] and Path(meta["sentinel"]).exists() and _valid_now(meta):
        append(ledger, step, "SKIP", 0, log, meta["sentinel"], "PASS")
        return "SKIP"
    append(ledger, step, "START", 0, log, meta["sentinel"], "NONE")
    return "RUN"


def mark(ledger: PathLike, step: str, want: str, *, sentinel=None, assert_cmd: Optional[str] = None,
         min_size: Optional[int] = None, log=None, rc: Optional[int] = None) -> str:
    """Terminal row OK|FAIL; a requested OK is recorded as FAIL when a declared assertion fails ("the step said OK"
    must mean something). Returns the recorded status."""
    return _mark(ledger, step, want, _meta(sentinel, assert_cmd, min_size), log, rc)[0]


def _mark(ledger: PathLike, step: str, want: str, meta: Dict, log, rc: Optional[int]) -> tuple:
    if want not in ("OK", "FAIL"):
        raise LedgerError(f"mark needs OK or FAIL, got {want!r}")
    valid = _valid_now(meta, rc, log)
    verdict = "NONE" if valid is None else ("PASS" if valid else "FAIL")
    status = "FAIL" if verdict == "FAIL" else want
    append(ledger, step, status, _since_start(ledger, step), log, meta["sentinel"], verdict)
    return status, verdict


def step(ledger: PathLike, name: str, cmd: Union[str, Sequence[str]], *, sentinel=None,
         assert_cmd: Optional[str] = None, min_size: Optional[int] = None, log, no_skip: bool = False) -> Dict:
    """Run one shell step under the ledger: SKIP on a validated sentinel, else START, run `cmd` (output to `log`),
    then OK|FAIL with the assertion verdict. {status, rc, assert}."""
    kw = {"sentinel": sentinel, "assert_cmd": assert_cmd, "min_size": min_size}
    if begin(ledger, name, log=log, no_skip=no_skip, **kw) == "SKIP":
        return {"status": "SKIP", "rc": None, "assert": "PASS"}
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as f:  # every execution appends under its own header: an earlier run's log is evidence
        f.write(f"=== step {name} START {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===\n".encode())
        f.flush()
        rc = subprocess.run(cmd, shell=isinstance(cmd, str), stdin=subprocess.DEVNULL, stdout=f,
                            stderr=subprocess.STDOUT).returncode
    status, verdict = _mark(ledger, name, "OK" if rc == 0 else "FAIL", _meta(**kw), log, rc)
    return {"status": status, "rc": rc, "assert": verdict}


# ---------------------------------------------------------------- scaffold
DRIVER = '''#!/usr/bin/env python3
"""Driver {name} - generated by `nexus-lite job scaffold`. Launch detached:
    nexus-lite job start {name} --cwd $PWD -- python {file}
Resume after a partial run (steps whose sentinel passes its assertion now are skipped):
    RESUME=1 nexus-lite job start {name} --force --cwd $PWD -- python {file}
A step failure is recorded in the ledger, not fatal; edit the STEPS block below."""
import os
import sys
from pathlib import Path

from nexus_lite import jobs, ledger

OUT = Path(os.environ.get("OUT", {outdir!r})).resolve()
LOG_DIR = OUT / "logs"
LEDGER = LOG_DIR / "{name}_status.tsv"
RESUME = os.environ.get("RESUME") == "1"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def run_step(step, cmd, *, sentinel, min_size=None, assert_cmd=None):
    """One START + one OK|FAIL row per execution; with RESUME=1 a step is skipped only when its sentinel passes
    its assertion now (an unasserted sentinel re-runs). Write outputs atomically: tmp beside the sentinel, then
    ledger.commit(tmp, sentinel)."""
    r = ledger.step(LEDGER, step, cmd, sentinel=sentinel, min_size=min_size, assert_cmd=assert_cmd,
                    log=LOG_DIR / f"{{step}}.log", no_skip=not RESUME)
    print(f"[{{step}}] {{r['status']}} rc={{r['rc']}} assert={{r['assert']}}", flush=True)
    return r


def preflight(*paths):
    missing = [str(p) for p in paths if not Path(p).exists()]
    if missing:
        print("[preflight] MISSING " + " ".join(missing), file=sys.stderr)
        sys.exit(1)
    print("[preflight] inputs present", flush=True)


def our_cores_free(quota, need):
    """Gate on OUR core quota, not the machine load average (other tenants' cores were never ours).
    quota and need are your values; there is no default."""
    return jobs.load(quota, need=need)["ok"]


# ---------------- EDIT BELOW: inputs, then one run_step per step ----------------
# preflight(Path("data/input.csv"))
# run_step("extract", ["python", "extract.py", "--out", str(OUT / "features.parquet")],
#          sentinel=OUT / "features.parquet", min_size=1024)
# run_step("model", ["python", "model.py"], sentinel=OUT / "result.csv",
#          assert_cmd='test $(wc -l < "$SENTINEL") -gt 1')
# --------------------------------------------------------------------------------
print(f"[{name}] done - ledger: {{LEDGER}}", flush=True)
'''


def scaffold(name: str, out: PathLike, outdir: str = "out", force: bool = False) -> Path:
    """Write a driver script (preflight, ledger, validated-sentinel resume, our-quota gate) to `out`; an existing
    file (an edited STEPS block) is kept unless `force`."""
    path = Path(out)
    if path.exists() and not force:
        raise LedgerError(f"{path} exists; use force to overwrite it")
    path.write_text(DRIVER.format(name=name, file=path.name, outdir=outdir), encoding="utf-8")
    path.chmod(0o755)
    return path
