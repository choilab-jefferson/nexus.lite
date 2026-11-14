"""nexus_lite.ledger: one ledger row per step; a step is skipped only when its sentinel validates now."""
import os
import threading

import pytest

from nexus_lite import ledger


def test_rows_have_a_header_and_seven_columns(tmp_path):
    led = tmp_path / "run" / "status.tsv"
    ledger.append(led, "s1", "START", 0, "s1.log", "out.csv", "NONE")
    ledger.append(led, "s1", "OK", 3, "s1.log", "out.csv", "PASS")
    text = led.read_text().splitlines()
    assert text[0] == ledger.HEADER and text[1].startswith("#utc\tstep")
    rows = ledger.rows(led)
    assert [(r["step"], r["status"], r["assert"]) for r in rows] == [("s1", "START", "NONE"), ("s1", "OK", "PASS")]
    assert all(len(r) == 7 for r in rows)


def test_tabs_and_newlines_in_fields_never_break_a_row(tmp_path):
    led = tmp_path / "status.tsv"
    ledger.append(led, "s", "OK", 0, "a\tb\nc", "", "NONE")
    (r,) = ledger.rows(led)
    assert r["logpath"] == "a b c" and r["sentinel"] == "-"


def test_a_torn_last_row_is_terminated_not_merged(tmp_path):
    led = tmp_path / "status.tsv"
    ledger.append(led, "a", "START", 0, "-", "-", "NONE")
    with open(led, "a") as f:
        f.write("2026-01-01T00:00:00Z\ta\tOK")         # a crash mid-write: no newline, too few columns
    ledger.append(led, "b", "START", 0, "-", "-", "NONE")
    assert [r["step"] for r in ledger.rows(led)] == ["a", "b"]    # the torn row is dropped, b is intact


def test_concurrent_appends_keep_every_row(tmp_path):
    led = tmp_path / "status.tsv"
    ts = [threading.Thread(target=lambda i=i: [ledger.append(led, f"s{i}", "START", 0, "-", "-", "NONE")
                                               for _ in range(20)]) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(ledger.rows(led)) == 100


def test_commit_renames_atomically_in_the_same_directory(tmp_path):
    out = tmp_path / "d" / "out.csv"
    out.parent.mkdir()
    tmp = out.parent / ".out.csv.tmp"
    tmp.write_text("a,b\n")
    ledger.commit(tmp, out)
    assert out.read_text() == "a,b\n" and not tmp.exists()
    other = tmp_path / "elsewhere.tmp"
    other.write_text("x")
    with pytest.raises(ledger.LedgerError):
        ledger.commit(other, out)                          # not beside the sentinel: refused


# ---------------------------------------------------------------- begin / mark / step
def test_step_runs_then_skips_on_a_validated_sentinel(tmp_path):
    led, out, log = tmp_path / "status.tsv", tmp_path / "out.txt", tmp_path / "logs" / "s1.log"
    cmd = ["sh", "-c", f"echo hello > {out}"]
    r = ledger.step(led, "s1", cmd, sentinel=out, min_size=1, log=log)
    assert r == {"status": "OK", "rc": 0, "assert": "PASS"} and "s1" in [x["step"] for x in ledger.rows(led)]
    r = ledger.step(led, "s1", cmd, sentinel=out, min_size=1, log=log)
    assert r["status"] == "SKIP"
    assert [x["status"] for x in ledger.rows(led)] == ["START", "OK", "SKIP"]


def test_an_unasserted_sentinel_never_skips_and_a_failed_assertion_turns_ok_into_fail(tmp_path):
    led, out = tmp_path / "status.tsv", tmp_path / "out.txt"
    out.write_text("old")
    r = ledger.step(led, "s", ["true"], sentinel=out, log=tmp_path / "s.log")
    assert r["status"] == "OK" and r["assert"] == "NONE"                   # ran again: no assertion declared
    r = ledger.step(led, "t", ["true"], sentinel=tmp_path / "never.txt", min_size=1, log=tmp_path / "t.log")
    assert r == {"status": "FAIL", "rc": 0, "assert": "FAIL"}               # rc 0 but no output: FAIL


def test_a_failing_command_is_fail_with_its_rc_and_output_goes_to_the_log(tmp_path):
    led, log = tmp_path / "status.tsv", tmp_path / "f.log"
    r = ledger.step(led, "f", ["sh", "-c", "echo boom; exit 4"], log=log)
    assert r == {"status": "FAIL", "rc": 4, "assert": "NONE"} and "boom" in log.read_text()


def test_begin_and_mark_for_an_agent_step(tmp_path):
    led, out = tmp_path / "status.tsv", tmp_path / "o.txt"
    assert ledger.begin(led, "a", sentinel=out, min_size=1) == "RUN"
    out.write_text("x")
    assert ledger.mark(led, "a", "OK", sentinel=out, min_size=1) == "OK"
    assert ledger.begin(led, "a", sentinel=out, min_size=1) == "SKIP"
    assert ledger.begin(led, "a", sentinel=out, min_size=1, no_skip=True) == "RUN"


def test_min_size_must_be_positive_and_not_with_assert(tmp_path):
    for kw in ({"min_size": 0}, {"min_size": -3}, {"min_size": 1, "assert_cmd": "true"}):
        with pytest.raises(ledger.LedgerError):
            ledger.begin(tmp_path / "l.tsv", "x", sentinel=tmp_path / "o", **kw)


# ---------------------------------------------------------------- CLI: nexus-lite job ledger ... / scaffold
import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _cli(tmp_path, *args, env=None):
    e = {k: v for k, v in os.environ.items() if k != "RUN_DIR"}
    e.update({"PYTHONPATH": str(ROOT), "NEXUS_LITE_DIR": str(tmp_path / ".nexus"), **(env or {})})
    return subprocess.run([PY, "-m", "nexus_lite", "job", *args], capture_output=True, text=True, env=e,
                          cwd=tmp_path, timeout=120)


def test_cli_ledger_step_needs_a_ledger_and_writes_rows(tmp_path):
    out = tmp_path / "o.txt"
    assert _cli(tmp_path, "ledger", "step", "s", "--", "true").returncode == 2        # no --ledger, no RUN_DIR
    r = _cli(tmp_path, "ledger", "step", "s", "--sentinel", str(out), "--min-size", "1", "--log",
             str(tmp_path / "s.log"), "--", "sh", "-c", f"echo x > {out}", env={"RUN_DIR": str(tmp_path / "run")})
    assert r.returncode == 0 and r.stdout.startswith("[s] OK")
    assert [x["status"] for x in ledger.rows(tmp_path / "run" / "status.tsv")] == ["START", "OK"]


def test_cli_begin_mark_commit(tmp_path):
    led, out = tmp_path / "l.tsv", tmp_path / "o.txt"
    assert _cli(tmp_path, "ledger", "begin", "a", "--ledger", str(led), "--sentinel", str(out),
                "--min-size", "1").returncode == 1                                       # RUN
    tmp = tmp_path / ".o.tmp"
    tmp.write_text("data")
    assert _cli(tmp_path, "ledger", "commit", str(tmp), str(out)).returncode == 0
    assert _cli(tmp_path, "ledger", "mark", "a", "OK", "--ledger", str(led), "--sentinel", str(out),
                "--min-size", "1").returncode == 0
    assert _cli(tmp_path, "ledger", "begin", "a", "--ledger", str(led), "--sentinel", str(out),
                "--min-size", "1").returncode == 0                                       # SKIP


def test_scaffold_writes_a_driver_that_resumes(tmp_path):
    drv = tmp_path / "run_demo.py"
    assert _cli(tmp_path, "scaffold", "demo", "--out", str(drv)).returncode == 0
    text = drv.read_text()
    assert "EDIT BELOW" in text and "nexus_lite" in text
    text = text.replace("# ---------------- EDIT BELOW", (
        "run_step('one', ['sh', '-c', f'echo 1 > {OUT}/one.txt'], sentinel=OUT / 'one.txt', min_size=1)\n"
        "# ---------------- EDIT BELOW"))
    drv.write_text(text)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "OUT": str(tmp_path / "out")}
    r1 = subprocess.run([PY, str(drv)], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)
    r2 = subprocess.run([PY, str(drv)], capture_output=True, text=True, env={**env, "RESUME": "1"}, cwd=tmp_path,
                        timeout=60)
    assert "[one] OK" in r1.stdout and "[one] SKIP" in r2.stdout, (r1.stdout, r1.stderr, r2.stdout, r2.stderr)


# ---------------------------------------------------------------- Cowork 114
def test_a_rerun_appends_to_the_step_log_with_an_execution_header(tmp_path):
    led, log = tmp_path / "l.tsv", tmp_path / "s.log"
    ledger.step(led, "s", ["sh", "-c", "echo first"], log=log)
    ledger.step(led, "s", ["sh", "-c", "echo second"], log=log)
    text = log.read_text()
    assert "first" in text and "second" in text and text.count("=== step s START") == 2


def test_scaffold_refuses_to_overwrite_a_driver_without_force(tmp_path):
    drv = tmp_path / "run_x.py"
    ledger.scaffold("x", drv)
    drv.write_text("# my edited steps\n")
    with pytest.raises(ledger.LedgerError):
        ledger.scaffold("x", drv)
    assert drv.read_text() == "# my edited steps\n"
    ledger.scaffold("x", drv, force=True)
    assert "EDIT BELOW" in drv.read_text()
    assert _cli(tmp_path, "scaffold", "x", "--out", str(drv)).returncode == 2
