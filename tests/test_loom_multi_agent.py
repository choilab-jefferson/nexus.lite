import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest

from nexus_lite.demos import loom_multi_agent as m

# Fake `loom` binary: behaviour per provider from cfg.json beside it, so every failure
# mode is injected deterministically. No network, no real loom, no API keys.
FAKE = r'''#!{py}
import json, os, re, signal, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "cfg.json")))
a = sys.argv[1:]
if a and a[0] in ("--version", "version", "capabilities"):
    sys.exit(2)   # the fake models a loom with no --version
def opt(n):
    return a[a.index(n) + 1] if n in a else None
prompt, prov, ws = opt("-p"), opt("--provider"), opt("--workspace")
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({{"prov": prov, "allow": [a[i + 1] for i, x in enumerate(a) if x == "--allow-cmd"],
                        "has_key": "ANTHROPIC_API_KEY" in os.environ,
                        "argv_has_key": any("sk-secret" in x for x in a)}}) + "\n")
with open(os.path.join(here, "env.json"), "w") as f:
    json.dump(sorted(os.environ), f)
mode = cfg.get("modes", {{}}).get(prov, "ok")
if mode == "fail_reviewer" and "responsible for failures" in prompt:
    sys.stderr.write(cfg.get("stderr", "reviewer exploded") + "\n")
    print(json.dumps({{"output": "boom"}})); sys.exit(1)
if mode == "fail_reviewer":
    mode = "ok"
if mode == "loud_fail":
    sys.stderr.write(cfg.get("stderr", "") + "\n")
    print(json.dumps({{"output": "boom"}})); sys.exit(1)
if mode == "needs_key" and os.environ.get("ANTHROPIC_API_KEY") != "good-key":
    print(json.dumps({{"output": "401 bad key"}})); sys.exit(1)
if mode == "fail":
    print(json.dumps({{"output": "boom"}})); sys.exit(1)
if mode == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "bad":
    print("<html>gateway error</html>"); sys.exit(0)
if mode == "sleep":
    time.sleep(30)
if "Split this spec" in prompt:
    print(json.dumps({{"output": cfg.get("split_reply", "not a list"), "usage": {{"total_tokens": 7}}}})); sys.exit(0)
mm = re.search(r"create `([\w.]+)`", prompt)
if mm and mm.group(1) in cfg["files"] and mode != "nowrite":
    open(os.path.join(ws, mm.group(1)), "w").write(cfg["files"][mm.group(1)])
print(json.dumps({{"output": "done", "usage": {{"total_tokens": 7}}}}))
'''


@pytest.fixture
def fakeloom(tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    p = d / "loom"
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    good = {k: v[1] for k, v in m.MOCK_FILES.items()}

    def configure(modes=None, files=None, split_reply=None, stderr=None):
        cfg = {"modes": modes or {}, "files": files if files is not None else good}
        if split_reply is not None:
            cfg["split_reply"] = split_reply
        if stderr is not None:
            cfg["stderr"] = stderr
        (d / "cfg.json").write_text(json.dumps(cfg))

    configure()
    monkeypatch.setenv("LOOM_BIN", str(p))
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    configure.dir = d
    configure.calls = lambda: [json.loads(x) for x in (d / "calls.jsonl").read_text().splitlines()]
    return configure


def _quiet(*_a, **_k):
    pass


def _build(tmp_path, providers, extra=None, **kw):
    kw.setdefault("baseline", False)
    kw.setdefault("backoff", 0)
    kw.setdefault("out", _quiet)
    return m.build(providers, os.environ["LOOM_BIN"], extra or {}, sandbox=str(tmp_path / "sb"), **kw)


# ------------------------------------------------------------------ basics
def test_pick_providers_falls_back_to_mock():
    assert m.pick_providers({}) == ["mock"]
    assert m.pick_providers({"GEMINI_API_KEY": "x"}) == ["gemini"]


def test_notebook_has_no_outputs():
    nb = json.loads(Path("notebooks/loom_multi_agent_colab.ipynb").read_text())
    assert all(not c.get("outputs") and c.get("execution_count") is None
               for c in nb["cells"] if c["cell_type"] == "code")


def test_secrets_env_reads_env_outside_colab(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sekret")
    assert m.secrets_env() == {"ANTHROPIC_API_KEY": "sekret"}


# ------------------------------------------------------------------ redaction
@pytest.mark.parametrize("raw", [
    "sk-ant-api03-AbCdEf_123-xyz", "key AIza" "SyD-1234567890abcdefg end", "ANTHROPIC_API_KEY=hunter2hunter2",
    "GEMINI_API_KEY: abc123", "Authorization: Bearer eyJhbGciOi.payload.sig", "https://x/y?key=abcd1234efgh"])
def test_redact_patterns(raw):
    out = m.redact(raw)
    assert "[REDACTED]" in out
    for secret in ("AbCdEf", "AIzaSyD-123", "hunter2", "abc123", "eyJhbGciOi", "abcd1234efgh"):
        assert secret not in out


def test_redact_exact_secret_values():
    assert "plainsecretvalue" not in m.redact("oops plainsecretvalue here", ["plainsecretvalue"])


# ------------------------------------------------------------------ agent(): failure modes
def test_agent_timeout_is_status_not_exception(fakeloom, tmp_path, monkeypatch):
    fakeloom(modes={"mock": "sleep"})
    r = m.agent(os.environ["LOOM_BIN"], "w", "mock", "hi", str(tmp_path), {}, timeout=0.5)
    assert r.status == "timeout" and not r.ok and "timed out" in r.err


def test_agent_oserror_is_status_not_exception(tmp_path):
    r = m.agent(str(tmp_path / "no-such-loom"), "w", "mock", "hi", str(tmp_path), {})
    assert r.status == "unavailable" and not r.ok


@pytest.mark.parametrize("mode,status", [("crash", "crash"), ("bad", "bad_output"), ("fail", "failed")])
def test_agent_failure_statuses(fakeloom, tmp_path, mode, status):
    fakeloom(modes={"mock": mode})
    r = m.agent(os.environ["LOOM_BIN"], "w", "mock", "hi", str(tmp_path), {})
    assert r.status == status and not r.ok
    if mode != "fail":  # non-JSON raw output is never echoed (may contain provider text)
        assert "gateway" not in r.text and "gateway" not in r.err


def test_agent_auth_error_has_own_status(fakeloom, tmp_path):
    fakeloom(modes={"anthropic": "needs_key"})
    r = m.agent(os.environ["LOOM_BIN"], "w", "anthropic", "hi", str(tmp_path), {"ANTHROPIC_API_KEY": "wrong"})
    assert r.status == "auth" and "401" in r.err


def test_agent_ok_reads_tokens_and_passes_allow_cmd(fakeloom, tmp_path):
    r = m.agent(os.environ["LOOM_BIN"], "reviewer", "mock", "hi", str(tmp_path), {}, 6, allow=("py -m pytest",))
    assert r.ok and r.tokens == 7
    assert fakeloom.calls()[-1]["allow"] == ["py -m pytest"]


def test_agent_unpinned_flag_not_used(fakeloom, tmp_path, monkeypatch):
    """loom has no --max-total-tokens flag use here; passing it made v0.1.1 exit 2."""
    seen = []
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **k: seen.append(cmd) or (_ for _ in ()).throw(OSError()))
    m.agent("loom", "w", "mock", "hi", str(tmp_path), {})
    assert "--max-total-tokens" not in seen[0]


def test_loom_version_falls_back_with_reason(fakeloom):
    assert m.loom_version(os.environ["LOOM_BIN"]).startswith("n/a (")


# ------------------------------------------------------------------ failure handling: never PASS on a failed agent
def _reviewer_fails(mod, tmp_path):
    """Hidden-test-agnostic: all workers write good files, but the gemini reviewer always fails."""
    res = mod.build(["gemini"], os.environ["LOOM_BIN"], {}, sandbox=str(tmp_path / "sb"), baseline=False, out=_quiet)
    return res.get("ok", res["passed"])  # old module has no "ok": its verdict is "passed"


def test_failed_reviewer_is_not_pass(fakeloom, tmp_path):
    """FAILS on the pre-fix module (it reported PASS because only pytest was consulted)."""
    fakeloom(modes={"gemini": "fail_reviewer"})
    assert _reviewer_fails(m, tmp_path) is False


def test_failed_reviewer_main_exit_and_summary(fakeloom, capsys, tmp_path):
    fakeloom(modes={"gemini": "fail_reviewer"}, split_reply=json.dumps(m.DEFAULT_SPLIT))
    rc = m.main(["--providers", "gemini", "--sandbox", str(tmp_path / "sb"), "--no-baseline", "--retries", "0"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: FAIL (reviewer: gemini status=failed" in out and "RESULT: PASS" not in out
    assert "reviewer exploded" in out  # stderr summary surfaced per agent


def test_failed_worker_final_attempt_is_not_pass(fakeloom, tmp_path):
    # round 0 good; hidden passes... but one module's only writer provider never writes
    fakeloom(modes={"gemini": "nowrite"})
    res = _build(tmp_path, ["gemini"], max_rounds=1)
    assert not res["ok"] and any("worker for" in f for f in res["failures"])


def test_failed_orchestrator_is_failure_even_if_fixed_split_works(fakeloom, tmp_path):
    fakeloom(modes={"anthropic": "fail"})
    res = _build(tmp_path, ["anthropic"], max_rounds=1, retries=0)
    assert not res["ok"] and any(f.startswith("orchestrator") for f in res["failures"])


def test_fake_key_in_stderr_never_reaches_logs_json_or_sandbox(fakeloom, monkeypatch, capsys, tmp_path):
    secret = "sk-ant-api03-FAKEKEYabcdef123456"
    fakeloom(modes={"anthropic": "loud_fail"}, stderr=f"auth failed for ANTHROPIC_API_KEY={secret} Authorization: Bearer tok123456789")
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    log, js = tmp_path / "run.log", tmp_path / "run.json"
    rc = m.main(["--providers", "anthropic", "--sandbox", str(tmp_path / "sb"), "--retries", "0",
                 "--log-file", str(log), "--json-out", str(js)])
    cap = capsys.readouterr()
    assert rc == 1
    blobs = [cap.out, cap.err, log.read_text(), js.read_text()]
    for root, _d, names in os.walk(tmp_path):
        if "fake" in root.split(os.sep):
            continue  # the fake loom's own env/call dumps are test scaffolding
        blobs += [Path(os.path.join(root, n)).read_text(errors="ignore") for n in names]
    for b in blobs:
        assert "FAKEKEYabcdef" not in b and "tok123456789" not in b
    assert "[REDACTED]" in log.read_text()


# ------------------------------------------------------------------ retry + provider fallback (mock, deterministic)
def test_retry_then_success_on_same_provider(fakeloom, tmp_path, monkeypatch):
    real = m.agent
    state = {"n": 0}

    def flaky(loom, role, prov, *a, **k):
        if role == "worker-1":
            state["n"] += 1
            if state["n"] == 1:
                return m.AgentResult(role, prov, "timeout", err="timed out after 1s")
        return real(loom, role, prov, *a, **k)
    monkeypatch.setattr(m, "agent", flaky)
    res = _build(tmp_path, ["gemini"], retries=1)
    w1 = [(a.attempt, a.status) for a in res["agents"] if a.role == "worker-1"]
    assert w1 == [(1, "timeout"), (2, "ok")] and res["ok"]


def test_retries_are_bounded(fakeloom, tmp_path):
    fakeloom(modes={"gemini": "fail"})
    res = _build(tmp_path, ["gemini"], retries=2, max_rounds=1)
    assert [a.attempt for a in res["agents"] if a.role == "orchestrator"] == [1, 2, 3]


def test_mock_bad_key_skips_retry_and_falls_back():
    res = m.build(["mock", "mock2"], "", {}, baseline=False, out=_quiet, backoff=0, retries=2,
                  inject=m.Inject(bad_key={"worker-2"}))
    w2 = [(a.provider, a.attempt, a.status) for a in res["agents"] if a.role == "worker-2"]
    assert w2 == [("mock2", 1, "auth"), ("mock", 1, "ok")]       # auth: no retry; then fallback
    assert res["ok"] and not res["failures"]


def test_mock_timeout_retries_then_falls_back():
    res = m.build(["mock", "mock2"], "", {}, baseline=False, out=_quiet, backoff=0, retries=1,
                  inject=m.Inject(timeout={"reviewer": 1.0}))
    rv = [(a.provider, a.attempt, a.status) for a in res["agents"] if a.role == "reviewer"][:3]
    assert rv == [("mock", 1, "timeout"), ("mock", 2, "timeout"), ("mock2", 1, "ok")]
    assert res["ok"]


def test_single_provider_no_fallback_means_failure():
    res = m.build(["mock"], "", {}, baseline=False, out=_quiet, backoff=0, retries=1,
                  inject=m.Inject(bad_key={"reviewer"}))
    assert not res["ok"] and any(f.startswith("reviewer") for f in res["failures"])


def test_real_path_bad_key_injection_uses_wrong_key_only_on_primary(fakeloom, monkeypatch, tmp_path):
    fakeloom(modes={"anthropic": "needs_key"}, split_reply=json.dumps(m.DEFAULT_SPLIT))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "good-key")
    res = _build(tmp_path, ["anthropic", "gemini"], {"ANTHROPIC_API_KEY": "good-key"},
                 inject=m.Inject(bad_key={"worker-1"}))
    w1 = [(a.provider, a.status) for a in res["agents"] if a.role == "worker-1"]
    assert w1 == [("anthropic", "auth"), ("gemini", "ok")] and res["ok"]


# ------------------------------------------------------------------ build() with a fake loom
def test_build_provider_fallback_on_bad_key(fakeloom, monkeypatch, tmp_path):
    fakeloom(modes={"anthropic": "needs_key"}, split_reply=json.dumps(m.DEFAULT_SPLIT))
    res = _build(tmp_path, ["anthropic", "gemini"], {"ANTHROPIC_API_KEY": "wrong"})
    assert res["ok"]
    statuses = {(a.provider, a.status) for a in res["agents"]}
    assert ("anthropic", "auth") in statuses and ("gemini", "ok") in statuses


def test_build_missing_key_all_providers_fail(fakeloom, tmp_path):
    fakeloom(modes={"anthropic": "needs_key"})
    res = _build(tmp_path, ["anthropic"])
    assert not res["ok"] and all(not a.ok for a in res["agents"])
    assert not any(f.endswith(".py") for f in os.listdir(res["sandbox"]))


def test_build_bad_orchestrator_reply_uses_fixed_split(fakeloom, tmp_path):
    fakeloom(split_reply="sorry, no json here")
    res = _build(tmp_path, ["gemini"])
    assert res["ok"]
    assert set(m.FILES) <= set(os.listdir(res["sandbox"]))


def test_build_worker_crash_then_other_provider(fakeloom, tmp_path):
    fakeloom(modes={"anthropic": "crash"})
    res = _build(tmp_path, ["anthropic", "gemini"], retries=0)
    assert res["ok"] and any(a.status == "crash" for a in res["agents"])


def test_build_worker_wrote_no_file_is_no_file_status(fakeloom, tmp_path):
    fakeloom(modes={"gemini": "nowrite"})
    res = _build(tmp_path, ["gemini"], max_rounds=1, retries=0)
    assert not res["ok"]
    assert {a.status for a in res["agents"] if a.role.startswith("worker")} == {"no_file"}


def test_build_rework_uses_feedback_and_caps_rounds(fakeloom, tmp_path):
    bad = {k: v[0] for k, v in m.MOCK_FILES.items()}  # fake never fixes the flaws
    fakeloom(files=bad)
    res = _build(tmp_path, ["gemini"], max_rounds=3)
    assert not res["ok"] and res["rounds"] == 3
    reworked = [a.role for a in res["agents"] if re.match(r"worker-\d\.r\d", a.role)]
    assert reworked == ["worker-1.r1", "worker-4.r1", "worker-1.r2", "worker-4.r2"]  # only the failing modules


def test_reviewer_and_workers_get_no_command_allowance(fakeloom, tmp_path):
    _build(tmp_path, ["gemini"], max_rounds=1)
    allows = [c["allow"] for c in fakeloom.calls()]
    assert allows and not any(allows)  # hidden tests are run by the harness only


# ------------------------------------------------------------------ hidden tests stay hidden
def test_hidden_tests_not_in_sandbox_or_prompts(tmp_path, monkeypatch):
    prompts = []
    real = m.mock_agent

    def spy(role, prompt, *a, **k):
        prompts.append(prompt)
        return real(role, prompt, *a, **k)
    monkeypatch.setattr(m, "mock_agent", spy)
    res = m.build(["mock"], "", {}, sandbox=str(tmp_path / "sb"), out=_quiet, backoff=0, baseline_repair=1)
    marker = "hidden-suite-marker-7f3a91"
    assert res["rounds"] == 2 and len(prompts) > 8
    assert not any(marker in p for p in prompts)
    assert not any(n.startswith("test_h_") for n in os.listdir(res["sandbox"]))
    assert not os.path.abspath(res["hidden_dir"]).startswith(os.path.abspath(res["sandbox"]))
    for root, _d, names in os.walk(res["sandbox"]):
        assert not any(marker in Path(os.path.join(root, n)).read_text(errors="ignore") for n in names)
    rework = [p for p in prompts if "Acceptance tests (hidden" in p]
    assert rework
    for p in rework:   # feedback block = only `test_name: FailureKind` lines
        fb = re.split(r"(?:fix the file|rewrite what is needed):\n", p, maxsplit=1)[1].strip().splitlines()
        assert fb and all(re.fullmatch(r"test_\w+: \w+", ln) for ln in fb), fb
    assert not any("def test_" in p for p in prompts)


def test_mock_rework_loop_runs_and_baseline_counts_same_hidden_suite(tmp_path):
    res = m.build(["mock"], "", {}, sandbox=str(tmp_path / "sb"), out=_quiet, backoff=0, baseline_repair=1)
    assert res["rounds"] > 1 and res["ok"] and res["hidden"]["passed"] == res["hidden"]["total"] == m.HIDDEN_TOTAL
    b = res["baseline"]
    assert b["hidden"]["total"] == m.HIDDEN_TOTAL and b["repair"] and b["repair"][0]["round"] == 1
    r1 = m.run_hidden(b["sandbox"], res["hidden_dir"])
    assert r1["passed"] == m.HIDDEN_TOTAL                 # after repair
    # scripted attempt-0 baseline is the same flawed payload as round 0 (mock numbers are not evidence)
    assert b["passed"] is False and b["hidden"]["passed"] < m.HIDDEN_TOTAL


def test_hidden_suite_passes_reference_and_kills_mutants(tmp_path):
    def score(files):
        sb, hd = tmp_path / "sb", tmp_path / "hd"
        sb.mkdir(exist_ok=True)
        for f, src in files.items():
            (sb / f).write_text(src)
        m.write_hidden(str(hd))
        return m.run_hidden(str(sb), str(hd))
    good = {f: p[1] for f, p in m.MOCK_FILES.items()}
    assert score(good)["passed"] == m.HIDDEN_TOTAL
    mutants = [("ttlcache.py", "< self.ttl", "<= self.ttl"), ("intervals.py", "s <= out[-1][1]", "s < out[-1][1]"),
               ("money.py", "ROUND_HALF_EVEN", "ROUND_HALF_UP"), ("tokenizer.py", "casefold", "lower"),
               ("money.py", "base = total_cents // parts", "base = int(total_cents / parts)")]
    for f, old, new in mutants:
        bad = dict(good, **{f: good[f].replace(old, new)})
        assert bad[f] != good[f]
        assert score(bad)["failing"], (f, old)


# ------------------------------------------------------------------ main(): key hygiene
def test_main_never_leaks_key_but_child_env_has_it(fakeloom, monkeypatch, capsys, tmp_path):
    fakeloom(split_reply=json.dumps(m.DEFAULT_SPLIT))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-123")
    rc = m.main(["--providers", "anthropic", "--sandbox", str(tmp_path / "sb")])
    cap = capsys.readouterr()
    assert rc == 0
    assert "sk-secret-123" not in cap.out + cap.err
    calls = fakeloom.calls()
    assert calls and all(c["has_key"] for c in calls)           # reached the child env ...
    assert not any(c["argv_has_key"] for c in calls)           # ... but never argv
    for root, _d, names in os.walk(tmp_path / "sb"):
        for n in names:
            assert "sk-secret-123" not in Path(os.path.join(root, n)).read_text(errors="ignore")


# ------------------------------------------------------------------ ensure_loom checksum
def _blob_env(monkeypatch, checksum_ok):
    import hashlib
    blob = b"#!/bin/sh\necho loom\n"
    sums = hashlib.sha256(blob).hexdigest() if checksum_ok else "0" * 64
    monkeypatch.delenv("LOOM_BIN", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda n: "/usr/bin/go" if n == "go" else None)
    monkeypatch.setattr(m, "_get", lambda n: blob if n == m.ASSET else f"{sums}  {m.ASSET}\n".encode())
    calls = []
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    return calls


def test_ensure_loom_checksum_mismatch_refuses_install(monkeypatch, tmp_path):
    calls = _blob_env(monkeypatch, checksum_ok=False)
    dest = str(tmp_path / "loom")
    m.ensure_loom(dest)
    assert not os.path.exists(dest)                      # tampered blob never written
    assert calls[0][:2] == ["git", "clone"] and calls[1][:2] == ["go", "build"]  # source fallback


def test_ensure_loom_checksum_ok_installs_without_build(monkeypatch, tmp_path):
    calls = _blob_env(monkeypatch, checksum_ok=True)
    dest = str(tmp_path / "loom")
    assert m.ensure_loom(dest) == dest
    assert os.access(dest, os.X_OK) and calls == []


# ------------------------------------------------------------------ pure helpers
def test_blame_maps_failing_hidden_tests_to_modules():
    h = {"failing": [("money.py", "t", "x"), ("tokenizer.py", "t", "y")]}
    assert m.blame(h, m.FILES) == ["tokenizer.py", "money.py"]
    assert m.blame({"failing": [("*", "hidden suite", "did not complete")]}, m.FILES) == m.FILES


def test_parse_split_rejects_bad_shapes():
    assert m.parse_split(json.dumps(m.DEFAULT_SPLIT)) == m.DEFAULT_SPLIT
    assert m.parse_split("nope") is None
    assert m.parse_split('[{"file": "../x.py", "goal": "g"}]') is None
    assert m.parse_split(json.dumps(m.DEFAULT_SPLIT[:3])) is None  # must cover every module


# ------------------------------------------------------------------ mock path end to end (CI) + CLI
def test_mock_example_end_to_end_and_json(tmp_path, capsys):
    sb, js = tmp_path / "sb", tmp_path / "o.json"
    rc = m.main(["--providers", "mock", "--sandbox", str(sb), "--json-out", str(js)])
    out = capsys.readouterr().out
    assert rc == 0
    for f in m.FILES:
        assert (sb / f).is_file()
    assert "round 1: hidden 29/33 FAILED" in out and "round 2: hidden 33/33 PASSED" in out
    assert "baseline (single agent, one shot): hidden=29/33" in out and "worker-4.r1" in out
    assert "RESULT: PASS" in out and "loom version: n/a (" in out and "tokens" in out
    run = json.loads(js.read_text())["runs"][0]
    assert run["ok"] and run["rounds"] == 2 and run["hidden"] == {"passed": 33, "total": 33, "failing": []}
    assert run["baseline"]["hidden"]["passed"] == 29 and run["agents"][0]["role"] == "orchestrator"


def test_tokens_na_has_reason_on_real_path(fakeloom, capsys, tmp_path):
    fakeloom(split_reply=json.dumps(m.DEFAULT_SPLIT))
    res = _build(tmp_path, ["gemini"], max_rounds=1)
    for a in res["agents"]:
        a.tokens = None
    assert "tokens=n/a (loom --json output carries no usage field)" in m.format_table(res)


def test_main_runs_flag_and_injection_banner(tmp_path, capsys):
    js = tmp_path / "o.json"
    rc = m.main(["--providers", "mock,mock2", "--runs", "2", "--no-baseline", "--json-out", str(js),
                 "--inject-bad-key", "worker-2", "--inject-timeout", "reviewer=1"])
    out = capsys.readouterr().out
    assert rc == 0 and "runs ok: 2/2" in out and "failure injection" in out
    runs = json.loads(js.read_text())["runs"]
    assert len(runs) == 2
    w2 = [(a["provider"], a["status"]) for a in runs[0]["agents"] if a["role"] == "worker-2"]
    assert w2[0] == ("mock2", "auth") and w2[-1][1] == "ok"


def test_example_script_runs_from_checkout_without_pythonpath(tmp_path):
    """Even when an older nexus-lite is installed, the script must use the checkout (or say what to do)."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    script = os.path.join(os.getcwd(), "examples", "loom_multi_agent.py")
    p = subprocess.run([sys.executable, script, "--providers", "mock", "--sandbox", str(tmp_path / "sb"),
                        "--no-baseline"], capture_output=True, text=True, env=env, timeout=120, cwd=tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "RESULT: PASS" in p.stdout


def test_agent_child_env_passes_loom_vars_only_allowlisted(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOM_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("UNRELATED_SECRET", "x")
    seen = {}

    def fake_run(cmd, **k):
        seen["env"] = k["env"]
        return m.subprocess.CompletedProcess(cmd, 0, '{"output": "done"}', "")
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert m.agent("loom", "worker-1", "anthropic", "p", str(tmp_path), {}).ok
    assert seen["env"]["LOOM_MAX_OUTPUT_TOKENS"] == "8192"
    assert "UNRELATED_SECRET" not in seen["env"]


# ------------------------------------------------------------------ audit defects
_WRONG = {"tokenizer.py": "def tokenize(text):\n    return ['LEAKME-424242']\n"}


def _score_files(tmp_path, files):
    sb, hd = tmp_path / "sb", tmp_path / "hd"
    sb.mkdir(exist_ok=True)
    for f, src in files.items():
        (sb / f).write_text(src)
    m.write_hidden(str(hd))
    return m.run_hidden(str(sb), str(hd))


def test_digest_has_names_and_kinds_only_no_expected_values(tmp_path):
    good = {f: p[1] for f, p in m.MOCK_FILES.items()}
    h = _score_files(tmp_path, dict(good, **_WRONG))
    assert h["failing"]
    d = m.digest(h)
    assert "424242" not in d and "assert" not in d and "==" not in d and "[" not in d
    assert all(re.fullmatch(r"test_\w+: \w+", ln) for ln in d.splitlines()), d


def test_run_hidden_is_isolated_from_sandbox_pytest_shadow(tmp_path):
    good = {f: p[1] for f, p in m.MOCK_FILES.items()}
    h = _score_files(tmp_path, dict(good, **{"pytest.py": "raise SystemExit('shadowed')\n",
                                             "json.py": "raise SystemExit('shadowed')\n"}))
    assert h["passed"] == m.HIDDEN_TOTAL and not h["failing"]


def test_zero_collected_tests_is_not_a_pass(tmp_path):
    sb, hd = tmp_path / "sb", tmp_path / "hd"
    sb.mkdir()
    hd.mkdir()
    h = m.run_hidden(str(sb), str(hd))   # empty hidden dir: pytest collects nothing
    assert h["passed"] == 0 and h["failing"] == [("*", "hidden suite", "no tests collected")]


def test_reviewer_failure_in_round1_is_recorded_when_later_round_succeeds(tmp_path, monkeypatch):
    real = m.mock_agent
    seen = {"n": 0}

    def flaky(role, prompt, *a, **k):
        if role == "reviewer":
            seen["n"] += 1
            if seen["n"] == 1:
                return m.AgentResult(role, "mock", "crash", "", None, 0.0, 1, "injected reviewer crash")
        return real(role, prompt, *a, **k)
    res_probe = m.build(["mock"], "", {}, sandbox=str(tmp_path / "p"), out=_quiet, backoff=0, baseline=False)
    assert res_probe["rounds"] > 1 and res_probe["warnings"] == []
    monkeypatch.setattr(m, "mock_agent", flaky)
    res = m.build(["mock"], "", {}, sandbox=str(tmp_path / "sb"), out=_quiet, backoff=0, baseline=False, retries=0)
    assert res["ok"] and res["rounds"] > 1
    assert len(res["warnings"]) == 1 and "round 1" in res["warnings"][0]
    assert "WARNING: reviewer failed in round 1" in m.format_table(res)
    assert m.to_json(res)["warnings"] == res["warnings"]


def test_docs_state_the_real_rework_limit():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rels = ["notebooks/loom_multi_agent_colab.ipynb", "examples/make_notebooks.py"]
    if os.path.exists(os.path.join(root, "docs/RELEASE.md")):   # docs/ is not in the public snapshot
        rels.append("docs/RELEASE.md")
    for rel in rels:
        txt = Path(os.path.join(root, rel)).read_text(encoding="utf-8")
        assert "max 3 rework" not in txt and "up to 3 rework" not in txt, rel
    assert "at most 2 rework rounds" in Path(os.path.join(root, "notebooks/loom_multi_agent_colab.ipynb")).read_text()


def test_baseline_without_files_is_no_file_and_continues_for_missing_files(tmp_path, monkeypatch):
    real = m.mock_agent
    calls = []

    def lossy(role, prompt, ws, file=None, attempt=0, *a, **k):
        if role.startswith("baseline") and not role.startswith("baseline.r"):
            calls.append(role)
            if role == "baseline":   # loom max_tokens: replies ok, writes nothing
                return m.AgentResult(role, "mock", "ok", "", None, 0.0, 0)
            assert "ONLY these still-missing files" in prompt
        return real(role, prompt, ws, file, attempt, *a, **k)
    monkeypatch.setattr(m, "mock_agent", lossy)
    res = m.build(["mock"], "", {}, sandbox=str(tmp_path / "sb"), out=_quiet, backoff=0)
    b = res["baseline"]
    assert calls[0] == "baseline" and len(calls) >= 2 and b["continuations"] >= 1
    first = [a for a in b["attempts"] if a.role == "baseline"][0]
    assert first.status == "no_file" and "did not write" in first.err   # not reported as ok
    assert b["hidden"]["total"] == m.HIDDEN_TOTAL and "continuation call(s)" in m.format_table(res)


def test_one_collection_error_does_not_zero_the_other_modules(tmp_path):
    good = {f: p[1] for f, p in m.MOCK_FILES.items()}
    h = _score_files(tmp_path, dict(good, **{"tokenizer.py": ""}))   # empty file: ImportError at collection
    assert 0 < h["passed"] < m.HIDDEN_TOTAL and h["failing"]
    assert {mod for mod, _n, _k in h["failing"]} == {"tokenizer.py"}


def test_empty_worker_file_is_no_file(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("x = 1\n")
    assert not m.written(str(tmp_path), "a.py") and m.written(str(tmp_path), "b.py")
    assert not m.written(str(tmp_path), "missing.py")


# ------------------------------------------------------------------ pinned release tag (mocked urlopen)
def _fake_release(monkeypatch, blob, sums, tag="v0.2.1"):
    """Mock urlopen: records URLs, serves asset + checksums only under `tag`."""
    import io
    urls = []

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(url, timeout=None):
        urls.append(url)
        if f"/{tag}/" not in url:
            raise OSError("404")
        return R(blob if url.endswith(m.ASSET) else sums)
    monkeypatch.setattr(m.urllib.request, "urlopen", fake)
    return urls


def test_default_tag_is_v0_2_1_and_used_in_urls(monkeypatch, tmp_path):
    import hashlib
    blob = b"#!/bin/sh\necho loom\n"
    sums = f"{hashlib.sha256(blob).hexdigest()}  {m.ASSET}\n".encode()
    monkeypatch.delenv("LOOM_BIN", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda n: None)
    urls = _fake_release(monkeypatch, blob, sums)
    dest = str(tmp_path / "loom")
    assert m.LOOM_TAG == "v0.2.1"
    assert m.ensure_loom(dest) == dest and Path(dest).read_bytes() == blob
    assert urls == [f"{m.LOOM_REL}/v0.2.1/{m.ASSET}", f"{m.LOOM_REL}/v0.2.1/checksums.txt"]


def test_release_checksum_mismatch_not_installed_under_tag(monkeypatch, tmp_path):
    blob = b"#!/bin/sh\necho loom\n"
    monkeypatch.delenv("LOOM_BIN", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda n: "/usr/bin/go" if n == "go" else None)
    _fake_release(monkeypatch, blob, f"{'0' * 64}  {m.ASSET}\n".encode())
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **k: None)
    dest = str(tmp_path / "loom")
    m.ensure_loom(dest)
    assert not os.path.exists(dest)
