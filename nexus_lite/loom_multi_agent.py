"""Colab-ready multi-agent BUILD on loom: orchestrator -> workers -> reviewer, vs a single-agent baseline.

Principle: multi-agent is only worth its cost where one agent cannot do the job in one shot.
So the same spec and the same HIDDEN acceptance tests are given to both, the single-agent
baseline is shown honestly, and nothing is rigged: results (including failures) are reported as they are.

Task: four independent python modules (tokenizer, intervals, TTL/LRU cache, money) with tricky
edge cases (see `loom_multi_task.SPEC`). An orchestrator splits the spec into one subtask per module;
workers write their module into a persistent sandbox (loom tool `write_file`). The HARNESS then runs
HIDDEN tests that live outside the sandbox and are never put in a prompt; a reviewer agent receives only
the failing test names + failure kinds (no test source, expected values or assertion messages), and the
failures go back to the responsible worker (up to 3 review rounds = at most 2 rework rounds). The reviewer's text is advisory; rework targets come from the harness.
The baseline gets the same spec, writes all modules in one shot and is scored by the same hidden tests;
optional self-repair rounds (`--baseline-repair N`) are reported separately.

    python examples/loom_multi_agent.py                       # auto: keys -> real, else mock
    python examples/loom_multi_agent.py --providers mock --json-out run.json
    python examples/loom_multi_agent.py --providers anthropic,gemini --runs 3 --json-out out.json
    python examples/loom_multi_agent.py --providers anthropic,gemini --inject-bad-key worker-2 \\
        --inject-timeout reviewer=1        # failure injection: retry + provider fallback paths

Failure handling: a failed orchestrator/worker/reviewer (final attempt) makes the run FAIL (exit 1),
never PASS. Provider/loom stderr is summarised per agent (last lines, secrets redacted). Each agent gets
`--retries` bounded retries (not for auth errors), then falls back to the next provider.
Failure injection applies to an agent's PRIMARY provider only, so fallback can recover.

Mock path (HONEST NOTE): loom's `mock` provider cannot call tools, so the offline path does not call loom:
an in-process scripted agent writes file payloads. Round 0 deliberately ships two flawed modules so the
review -> rework loop runs deterministically. Mock numbers are scripted, NOT evidence about any model.
Providers named `mock`, `mock2`, ... are scripted; `--providers mock,mock2` exercises fallback offline.

Stdlib only. API keys come ONLY from Colab Secrets (google.colab.userdata) or, outside Colab, from the
existing environment. They go to the loom child env and are never printed, logged or written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import loom_multi_task as _basic_task
from .loom_multi_task import (DEFAULT_SPLIT, FILES, HIDDEN_TESTS, HIDDEN_TOTAL, MOCK_FILES,  # noqa: F401
                              SPEC)

LOOM_TAG = os.environ.get("LOOM_TAG", "v0.2.1")
LOOM_REL = "https://github.com/choilab-jefferson/nexus.loom/releases/download"
LOOM_SRC = "https://github.com/choilab-jefferson/nexus.loom"
ASSET = "loom-linux-amd64"
KEYS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
MODELS = {"anthropic": "claude-haiku-5-5", "gemini": "gemini-3.8-flash"}
AGENT_TIMEOUT = 300
RETRY_BACKOFF = 1.0
NO_RETRY = ("auth", "unavailable")
NO_USAGE = "n/a (loom --json output carries no usage field)"


# ---------------------------------------------------------------- pluggable tasks
# A task is any object/module exposing NAME, SPEC, FILES, GOALS, DEFAULT_SPLIT, MOCK_FILES
# {file: (flawed_src, good_src)}, HIDDEN_TESTS {test_file: src}, HIDDEN_TOTAL. The default is `loom_multi_task`.
def available_tasks() -> dict:
    """name -> task object (the basic task plus the radiomics tasks)."""
    from .loom_radiomics_tasks import TASKS
    return {"basic": _basic_task, **TASKS}


def get_task(task=None):
    """None -> the default (basic) task; a task object passes through; a name is looked up."""
    if task is None:
        return _basic_task
    if not isinstance(task, str):
        return task
    tasks = available_tasks()
    if task not in tasks:
        raise SystemExit(f"unknown task {task!r}; available: {', '.join(tasks)}")
    return tasks[task]


# ---------------------------------------------------------------- redaction
_SECRET_RES = [re.compile(p) for p in (
    r"sk-ant-[A-Za-z0-9_\-]+", r"\bsk-[A-Za-z0-9_\-]{8,}", r"AIza[0-9A-Za-z_\-]{10,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{6,}", r"(?i)\bkey=[A-Za-z0-9_\-]{8,}")]
_KEYVAR_RE = re.compile(r"(?i)\b([A-Za-z0-9_]*_API_KEY)\b\s*[=:]\s*\S+")
_AUTH_RE = re.compile(r"(?i)\b(401|403)\b|unauthori[sz]ed|invalid[^\n]{0,24}key|authentication|api key")


def redact(text: str, secrets=()) -> str:
    """Replace known key shapes, `*_API_KEY=...` and exact secret values with [REDACTED]."""
    text = str(text)
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "[REDACTED]")
    text = _KEYVAR_RE.sub(lambda m: m.group(1) + "=[REDACTED]", text)
    for rx in _SECRET_RES:
        text = rx.sub(lambda m: "Bearer [REDACTED]" if m.group(0).lower().startswith("bearer")
                      else "[REDACTED]", text)
    return text


def err_summary(text: str, secrets=(), lines: int = 3, cap: int = 300) -> str:
    """Last few non-empty lines, one line, redacted, capped."""
    tail = [l.strip() for l in str(text).splitlines() if l.strip()][-lines:]
    return redact(" | ".join(tail), secrets)[:cap]


# ---------------------------------------------------------------- loom install / secrets
def _get(name: str) -> bytes:
    with urllib.request.urlopen(f"{LOOM_REL}/{LOOM_TAG}/{name}", timeout=60) as r:
        return r.read()


def ensure_loom(dest: str = "") -> str:
    """Return a loom path: LOOM_BIN / PATH, else checksum-verified release, else build from source."""
    found = os.environ.get("LOOM_BIN") or shutil.which("loom")
    if found:
        return found
    dest = dest or os.path.join(tempfile.mkdtemp(prefix="loom-bin-"), "loom")
    try:
        blob = _get(ASSET)
        want = [l.split()[0] for l in _get("checksums.txt").decode().splitlines() if l.endswith(ASSET)]
        if not want or hashlib.sha256(blob).hexdigest() != want[0]:
            raise RuntimeError("checksum mismatch")
        with open(dest, "wb") as f:
            f.write(blob)
        os.chmod(dest, 0o755)
        return dest
    except Exception as e:  # noqa: BLE001 - fall back to source build
        print(f"release install failed ({type(e).__name__}); building from source")
    if not shutil.which("go"):
        subprocess.run(["apt-get", "-qq", "install", "-y", "golang-go"], check=True, stdout=subprocess.DEVNULL)
    src = os.path.join(tempfile.mkdtemp(), "loom-src")
    subprocess.run(["git", "clone", "--depth", "1", "--branch", LOOM_TAG, LOOM_SRC, src], check=True)
    subprocess.run(["go", "build", "-o", dest, "./cmd/loom"], cwd=src, check=True)
    return dest


def loom_version(loom: str) -> str:
    """`loom --version` / `loom version`, else spec_version from `loom capabilities --json`, else n/a + reason."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    for args in (["--version"], ["version"]):
        try:
            p = subprocess.run([loom, *args], capture_output=True, text=True, timeout=10, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            return f"n/a ({type(e).__name__})"
        lines = (p.stdout or "").strip().splitlines()
        if p.returncode == 0 and lines:
            return redact(lines[0])[:80]
    try:
        p = subprocess.run([loom, "capabilities", "--json"], capture_output=True, text=True, timeout=10, env=env)
        caps = json.loads(p.stdout)
        if isinstance(caps, dict) and caps.get("spec_version"):
            return f"n/a (no --version/version command; capabilities spec_version={caps['spec_version']})"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return "n/a (loom has no --version or version command)"


def secrets_env() -> dict:
    """Keys from Colab Secrets (or already-set env outside Colab). Values are never printed."""
    out = {}
    try:
        from google.colab import userdata  # type: ignore
    except ImportError:
        userdata = None
    for k in KEYS.values():
        v = None
        if userdata is not None:
            try:
                v = userdata.get(k)
            except Exception:  # noqa: BLE001 - secret not set / access not granted
                v = None
        else:
            v = os.environ.get(k)
        if v:
            out[k] = v
    return out


def pick_providers(extra_env: dict) -> list:
    live = [p for p, k in KEYS.items() if k in extra_env]
    return live or ["mock"]


def is_mock(provider: str) -> bool:
    return provider.startswith("mock")


# ---------------------------------------------------------------- agents
@dataclass
class AgentResult:
    role: str
    provider: str
    status: str            # ok | failed | auth | timeout | unavailable | crash | bad_output | no_file
    text: str = ""
    tokens: object = None  # int when known, else None (printed as n/a)
    wall: float = 0.0
    rc: object = None
    err: str = ""          # short redacted provider/loom error summary
    attempt: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Inject:
    """Per-agent failure injection (role = base name, e.g. `worker-2`, `reviewer`, or `all`)."""
    bad_key: set = field(default_factory=set)
    timeout: dict = field(default_factory=dict)

    def match(self, role: str, spec) -> bool:
        base = role.split(".")[0]
        return base in spec or "all" in spec


def agent(loom, role, provider, prompt, ws, extra_env, max_turns=2, allow=(), timeout=None) -> AgentResult:
    """Run one loom agent in `ws`; never raises. Status names the failure mode, err summarises stderr."""
    cmd = [loom, "-p", prompt, "--provider", provider, "--workspace", ws, "--json",
           "--no-agents-md", "--no-stm", "--max-turns", str(max_turns)]
    for a in allow:
        cmd += ["--allow-cmd", a]
    if provider in MODELS:
        cmd += ["--model", MODELS[provider]]
    elif os.environ.get("LOOM_MODEL") and not is_mock(provider):
        cmd += ["--model", os.environ["LOOM_MODEL"]]
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "TMPDIR", "OLLAMA_HOST") or k.startswith("LOOM_")}  # e.g. LOOM_MAX_OUTPUT_TOKENS
    env.update(extra_env)
    secrets = list(extra_env.values())
    t0 = time.time()
    res = AgentResult(role, provider, "failed")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or AGENT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        res.status, res.err = "timeout", "timed out after %ss" % (timeout or AGENT_TIMEOUT)
    except OSError as e:
        res.status, res.text = "unavailable", type(e).__name__  # message may echo paths only
        res.err = res.text
    else:
        res.rc = p.returncode
        res.err = err_summary(p.stderr, secrets)
        try:
            r = json.loads(p.stdout)
            if not isinstance(r, dict):
                raise ValueError("not an object")
        except ValueError:
            # stdout is not echoed: may contain provider text
            res.status = "crash" if p.returncode < 0 else ("bad_output" if p.returncode == 0 else "failed")
            res.text = "no usable output (exit %d)" % p.returncode
        else:
            res.text = str(r.get("output", "")).strip()
            tok = (r.get("usage") or {}).get("total_tokens")
            res.tokens = tok if isinstance(tok, int) else None
            res.status = "ok" if p.returncode == 0 else "failed"
            if res.status == "failed" and not res.err:
                res.err = err_summary(res.text, secrets, lines=1)
        if res.status == "failed" and _AUTH_RE.search(res.err + " " + res.text):
            res.status = "auth"
    res.wall = time.time() - t0
    return res


def mock_agent(role, prompt, ws, file=None, attempt=0, timeout=None, bad_key=False, provider="mock",
               task=None) -> AgentResult:
    """Scripted offline agent: the HARNESS writes the payload (loom mock cannot call tools).
    Deterministic equivalents of auth error (bad_key) and timeout (injected timeout) are returned as statuses."""
    t0 = time.time()
    task = get_task(task)
    if bad_key:
        return AgentResult(role, provider, "auth", "", None, 0.0, 1, "401 invalid API key (mock)")
    if timeout is not None:
        return AgentResult(role, provider, "timeout", "", None, 0.0, None, "mock timed out after %ss" % timeout)
    base = role.split(".")[0]
    text = "mock reply"
    if base == "orchestrator":
        text = json.dumps(task.DEFAULT_SPLIT)
    elif file in task.MOCK_FILES:
        with open(os.path.join(ws, file), "w", encoding="utf-8") as f:
            f.write(task.MOCK_FILES[file][1 if attempt else 0])
        text = f"wrote {file}"
    elif base == "reviewer":
        text = "mock reviewer: see the failing hidden tests listed in the prompt"
    elif base == "baseline":
        for name, pair in task.MOCK_FILES.items():
            with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
                f.write(pair[1 if attempt else 0])
        text = "wrote " + ", ".join(task.MOCK_FILES)
    return AgentResult(role, provider, "ok", text, (len(prompt) + len(text)) // 4, time.time() - t0, 0)


def written(wdir: str, name: str) -> bool:
    """A file counts only if it exists and is non-empty (a truncated tool call can leave an empty file)."""
    p = os.path.join(wdir, name)
    return os.path.isfile(p) and os.path.getsize(p) > 0


@dataclass
class Runner:
    """Runs agents with failure injection, bounded retry and provider fallback; logs every attempt."""
    loom: str
    extra_env: dict
    inject: Inject = field(default_factory=Inject)
    retries: int = 1
    backoff: float = RETRY_BACKOFF
    log: list = field(default_factory=list)
    task: object = None

    def once(self, role, prov, prompt, wdir, file, attempt, turns, allow, primary) -> AgentResult:
        bad = primary and self.inject.match(role, self.inject.bad_key)
        tmo = None
        if primary:
            for spec, sec in self.inject.timeout.items():
                if role.split(".")[0] == spec or spec == "all":
                    tmo = sec
        if is_mock(prov):
            r = mock_agent(role, prompt, wdir, file, attempt, tmo, bad, prov, task=self.task)
        else:
            env = dict(self.extra_env)
            if bad and prov in KEYS:
                env[KEYS[prov]] = "sk-injected-invalid-key-000000"
            r = agent(self.loom, role, prov, prompt, wdir, env, turns, allow, tmo)
        if file and r.ok and not written(wdir, file):
            r.status, r.err = "no_file", "agent replied ok but did not write " + file
        return r

    def run(self, role, prompt, order, wdir, file=None, attempt=0, turns=8, allow=()) -> AgentResult:
        last = None
        for pi, prov in enumerate(order):
            for n in range(self.retries + 1):
                r = self.once(role, prov, prompt, wdir, file, attempt, turns, allow, pi == 0)
                r.attempt = n + 1
                self.log.append(r)
                if r.ok:
                    return r
                last = r
                if r.status in NO_RETRY:
                    break
                if n < self.retries and self.backoff:
                    time.sleep(self.backoff * 2 ** n)
        return last


# ---------------------------------------------------------------- hidden tests
HIDDEN_CONFTEST = ('import os, sys\n'
                   '# -I drops PYTHONPATH; append (never prepend) so a sandbox file cannot shadow stdlib or pytest\n'
                   'sys.path.append(os.environ["LOOM_HIDDEN_WS"])\n')


def write_hidden(dest: str, task=None) -> str:
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "conftest.py"), "w", encoding="utf-8") as f:
        f.write(HIDDEN_CONFTEST)
    for name, src in get_task(task).HIDDEN_TESTS.items():
        with open(os.path.join(dest, name), "w", encoding="utf-8") as f:
            f.write(src)
    return dest


def run_hidden(ws: str, hdir: str, timeout: int = 60, task=None) -> dict:
    """Run the hidden suite against `ws`. Returns {passed,total,failing:[(module.py,name,kind)]}.

    `kind` is the failure class only (e.g. AssertionError); never assertion messages, expected values or test source.
    pytest runs isolated (-I) so a sandbox pytest.py / json.py cannot shadow the real modules.
    """
    total = get_task(task).HIDDEN_TOTAL
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update(LOOM_HIDDEN_WS=ws)
    xml = os.path.join(hdir, "result.xml")
    if os.path.exists(xml):
        os.remove(xml)
    try:
        p = subprocess.run([sys.executable, "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", "--rootdir", hdir,
                            "--junitxml", xml, hdir], cwd=hdir, capture_output=True, text=True, timeout=timeout, env=env)
        root = ET.parse(xml).getroot()
    except (subprocess.TimeoutExpired, OSError, ET.ParseError) as e:
        return {"passed": 0, "total": total,
                "failing": [("*", "hidden suite", f"did not complete ({type(e).__name__})")]}
    failing, passed = [], 0
    for tc in root.iter("testcase"):
        bad = next((e for e in (tc.find("failure"), tc.find("error"), tc.find("skipped")) if e is not None), None)
        if bad is None:
            passed += 1
            continue
        stem = (tc.get("classname") or tc.get("name") or "").split(".")[0]
        mod = stem.replace("test_h_", "") + ".py"
        name = re.sub(r"\[.*", "", tc.get("name") or "collection")  # parametrize ids can carry values
        kind = bad.get("type") or bad.tag
        failing.append((mod, name, kind if re.fullmatch(r"[\w.]{1,60}", kind) else bad.tag))
    if not failing and passed == 0:  # zero collected tests must never count as a pass
        failing.append(("*", "hidden suite", "no tests collected"))
    return {"passed": passed, "total": total, "failing": failing}


def digest(hidden: dict, only=None, cap: int = 2500) -> str:
    """Failing test names + failure kind only (no test source, expected values or assertion messages)."""
    lines = [f"{n}: {m}" for mod, n, m in hidden["failing"] if only is None or mod == only or mod == "*.py"]
    return "\n".join(lines)[:cap]


def blame(hidden: dict, files) -> list:
    """Modules to rework = modules with failing hidden tests; every module if nothing maps."""
    hit = {mod for mod, _n, _m in hidden["failing"]}
    return [f for f in files if f in hit] or list(files)


def parse_split(text, task=None):
    """Orchestrator reply -> list of {file, goal} covering exactly the spec's modules, or None."""
    try:
        items = json.loads(text[text.index("["): text.rindex("]") + 1])
        assert isinstance(items, list) and items
        ok = [i for i in items if isinstance(i, dict) and isinstance(i.get("file"), str)
              and re.fullmatch(r"[A-Za-z_][\w]*\.py", i["file"]) and isinstance(i.get("goal"), str)]
        files = get_task(task).FILES
        assert len(ok) == len(items) and {i["file"] for i in ok} == set(files) and len(ok) == len(files)
        return ok
    except (ValueError, AssertionError, TypeError):
        return None


# ---------------------------------------------------------------- build
def build(providers, loom, extra_env, max_rounds=3, sandbox="", baseline=True, out=print,
          inject=None, retries=1, backoff=None, baseline_repair=0, task=None) -> dict:
    """Orchestrate the build. Returns a dict (ok, failures, sandbox, rounds, passed, hidden, agents, baseline)."""
    task = get_task(task)
    SPEC, FILES, DEFAULT_SPLIT, HIDDEN_TOTAL = task.SPEC, task.FILES, task.DEFAULT_SPLIT, task.HIDDEN_TOTAL
    secrets = list(extra_env.values())
    emit = out
    out = lambda s: emit(redact(s, secrets))  # noqa: E731 - every line leaves redacted
    ws = sandbox or tempfile.mkdtemp(prefix="loom-sandbox-")
    os.makedirs(ws, exist_ok=True)
    hdir = write_hidden(tempfile.mkdtemp(prefix="loom-hidden-"), task)  # outside the sandbox, never in a prompt
    out(f"sandbox (kept): {ws}")
    kw = dict(inject=inject or Inject(), retries=retries, backoff=RETRY_BACKOFF if backoff is None else backoff,
              task=task)
    rn = Runner(loom, extra_env, **kw)
    last = {}  # role base -> last AgentResult (final outcome per agent)

    def call(role, prompt, order, **k):
        r = rn.run(role, prompt, order, k.pop("wdir", ws), **k)
        last[role.split(".")[0]] = r
        return r

    o = call("orchestrator", 'Split this spec into one subtask per module. Reply with ONLY a JSON list of '
             '{"file": "<name>.py", "goal": "<what it must contain>"} objects, one per module. Spec: ' + SPEC,
             providers, turns=2)
    split = parse_split(o.text, task) if o.ok else None
    if split is None:
        out("orchestrator reply unusable; using fixed split")
        split = DEFAULT_SPLIT
    files = [s["file"] for s in split]
    goal = {s["file"]: s["goal"] for s in split}

    def work(file, i, attempt, feedback=""):
        order = [providers[(i + k) % len(providers)] for k in range(len(providers))]
        prompt = (f"You are worker-{i + 1}. Use the write_file tool to create `{file}` in the workspace "
                  f"(flat, no subdirectories). Overall spec:\n{SPEC}\nYour file: {file}: {goal[file]}.\n"
                  f"Other files: {', '.join(f for f in files if f != file)}. Write ONLY your file with write_file, "
                  "run NO shell commands (they are blocked), then reply 'done'.")
        if feedback:
            cur = ""
            if os.path.exists(os.path.join(ws, file)):
                cur = open(os.path.join(ws, file), encoding="utf-8").read()
            prompt += (f"\nYour previous `{file}`:\n{cur}\nAcceptance tests (hidden, source unavailable) that "
                       f"failed (name: failure kind); fix the file:\n{feedback}")
        return call(f"worker-{i + 1}" + (f".r{attempt}" if attempt else ""), prompt, order, file=file,
                    attempt=attempt)

    file_ok = {}
    for i, f in enumerate(files):
        file_ok[f] = work(f, i, 0).ok
    rounds, hidden = 0, {"passed": 0, "total": HIDDEN_TOTAL, "failing": []}
    rv_bad = []  # reviewer failures per round, kept even if a later round succeeds
    for rounds in range(1, max_rounds + 1):
        hidden = run_hidden(ws, hdir, task=task)
        rv = call("reviewer", "Hidden acceptance tests were run by the harness; the test source is not available to "
                  "you. Use no tools. Report which source files are responsible for failures, one line each. "
                  f"Failing tests:\n{digest(hidden) or '(none)'}", providers, turns=4)
        done = not hidden["failing"]
        if not rv.ok:
            rv_bad.append(f"reviewer failed in round {rounds}: {rv.provider} status={rv.status}"
                          + (f" ({rv.err})" if rv.err else ""))
        out(f"round {rounds}: hidden {hidden['passed']}/{hidden['total']} {'PASSED' if done else 'FAILED'}"
            f" (reviewer: {rv.status}{', ' + rv.err if rv.err else ''})")
        if done or rounds == max_rounds:
            break
        for f in blame(hidden, files):
            i = files.index(f)
            file_ok[f] = work(f, i, rounds, digest(hidden, f)).ok
    passed = not hidden["failing"]
    failures = [f"{r}: {a.provider} status={a.status}" + (f" ({a.err})" if a.err else "")
                for r, a in sorted(last.items()) if not a.ok and not r.startswith(("worker", "baseline"))]
    failures += [f"worker for {f}: last attempt failed" for f, ok in file_ok.items() if not ok]
    result = {"task": task.NAME, "sandbox": ws, "hidden_dir": hdir, "rounds": rounds, "passed": passed, "hidden": hidden,
              "failures": failures, "warnings": rv_bad, "ok": passed and not failures, "agents": rn.log,
              "wall": sum(a.wall for a in rn.log), "retries": retries}
    if baseline:
        brn = Runner(loom, extra_env, **kw)
        bws = tempfile.mkdtemp(prefix="loom-baseline-")
        bp = (f"Use write_file to create ALL of these files flat in the workspace: {', '.join(files)}. Spec:\n{SPEC}\n"
              "Run NO shell commands (they are blocked), then reply 'done'.")
        b = brn.run("baseline", bp, providers, bws, turns=12)
        miss = lambda: [f for f in files if not written(bws, f)]  # noqa: E731
        cont = 0
        # loom can stop on max_tokens mid tool call and still report success: a reply without files is no_file
        while b.status in ("ok", "no_file") and miss() and cont <= len(files):
            if b.status == "ok":
                b.status, b.err = "no_file", "agent replied ok but did not write: " + ", ".join(miss())
            if cont == len(files):
                break
            cont += 1
            b = brn.run(f"baseline.c{cont}", f"Use write_file to create ONLY these still-missing files, flat in the "
                        f"workspace: {', '.join(miss())}. Spec:\n{SPEC}\nRun NO shell commands (they are blocked), "
                        "then reply 'done'.", providers, bws, turns=12)
        if b.status == "ok" and miss():
            b.status, b.err = "no_file", "agent replied ok but did not write: " + ", ".join(miss())
        bh = run_hidden(bws, hdir, task=task)
        base = {"agent": b, "sandbox": bws, "hidden": bh, "passed": not bh["failing"], "repair": [],
                "wall": sum(a.wall for a in brn.log), "tokens": b.tokens, "continuations": cont}
        for k in range(1, baseline_repair + 1):
            if not b.ok or not bh["failing"]:
                break
            rb = brn.run(f"baseline.r{k}", bp + f"\nAcceptance tests (hidden, source unavailable) that failed in your "
                         f"previous attempt; rewrite what is needed:\n{digest(bh)}", providers, bws, attempt=k, turns=12)
            bh = run_hidden(bws, hdir, task=task)
            base["repair"].append({"round": k, "status": rb.status, "hidden": bh, "wall": rb.wall, "tokens": rb.tokens})
        base["attempts"] = brn.log
        result["baseline"] = base
    out("\n" + format_table(result))
    return result


def _tok(t) -> str:
    return str(t) if isinstance(t, int) else "n/a"


def format_table(result) -> str:
    rows = [("agent", "provider", "try", "status", "tokens", "wall_s", "error")]
    mock = any(is_mock(a.provider) for a in result["agents"])
    base = result.get("baseline")
    allr = result["agents"] + (base["attempts"] if base else [])
    for a in allr:
        rows.append((a.role, a.provider, str(a.attempt), a.status, _tok(a.tokens), f"{a.wall:.1f}", a.err[:70]))
    w = [max(len(r[i]) for r in rows) for i in range(7)]
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    errs = [f"  {a.role} [{a.provider}] {a.status}: {a.err}" for a in allr if not a.ok and a.err]
    if errs:
        lines += ["provider/loom errors (redacted, last lines):"] + errs
    counted = [a.tokens for a in result["agents"] if isinstance(a.tokens, int)]
    why = "mock estimate" if mock else NO_USAGE
    tot = sum(counted) if counted else why
    h = result["hidden"]
    lines.append(f"multi-agent: rounds={result['rounds']} hidden={h['passed']}/{h['total']} tokens={tot} "
                 f"agents={len(result['agents'])} wall={result['wall']:.1f}s")
    if base:
        bh = base["hidden"]
        mode = "one shot" if not base["continuations"] else f"{base['continuations']} continuation call(s) for unwritten files"
        lines.append(f"baseline (single agent, {mode}): hidden={bh['passed']}/{bh['total']} status={base['agent'].status} "
                     f"tokens={_tok(base['tokens']) if isinstance(base['tokens'], int) else why} wall={base['wall']:.1f}s")
        for r in base["repair"]:
            lines.append(f"baseline self-repair round {r['round']}: hidden={r['hidden']['passed']}/{r['hidden']['total']} "
                         f"status={r['status']} (reported separately from one shot)")
    if mock:
        lines.append("(mock: scripted payloads and a deterministic char/4 token estimate; not evidence about any model)")
    for wmsg in result.get("warnings", []):
        lines.append("WARNING: " + wmsg + (" (a later round succeeded)" if result["ok"] else ""))
    if result["ok"]:
        lines.append("RESULT: PASS")
    else:
        why_fail = result["failures"] or [f"hidden tests {h['passed']}/{h['total']} after {result['rounds']} round(s)"]
        lines.append("RESULT: FAIL (" + "; ".join(why_fail) + ")")
    return "\n".join(lines)


def to_json(result, loomv: str = "") -> dict:
    """JSON-safe run record (already redacted at the source: err summaries and statuses only)."""
    def ag(a):
        return {"role": a.role, "provider": a.provider, "attempt": a.attempt, "status": a.status, "ok": a.ok,
                "wall_s": round(a.wall, 3), "tokens": a.tokens if isinstance(a.tokens, int) else None,
                "tokens_note": None if isinstance(a.tokens, int) else NO_USAGE, "error": a.err}

    def hd(h):
        return {"passed": h["passed"], "total": h["total"], "failing": [list(x) for x in h["failing"]]}
    d = {"task": result.get("task", "basic"), "ok": result["ok"], "failures": result["failures"], "warnings": result.get("warnings", []), "rounds": result["rounds"], "loom_version": loomv,
         "hidden": hd(result["hidden"]), "agents": [ag(a) for a in result["agents"]],
         "multi_agent_wall_s": round(result["wall"], 3), "sandbox": result["sandbox"]}
    b = result.get("baseline")
    if b:
        d["baseline"] = {"status": b["agent"].status, "hidden": hd(b["hidden"]), "wall_s": round(b["wall"], 3),
                         "tokens": b["tokens"] if isinstance(b["tokens"], int) else None,
                         "attempts": [ag(a) for a in b["attempts"]],
                         "repair": [{"round": r["round"], "status": r["status"], "hidden": hd(r["hidden"]),
                                     "wall_s": round(r["wall"], 3)} for r in b["repair"]]}
    return d


_RADIOMICS_NAMES = ("radiomics-firstorder", "radiomics-texture", "radiomics-stats", "radiomics-suv")


def _kv(items, what):
    out = {}
    for it in items or []:
        role, _, val = it.partition("=")
        try:
            out[role] = float(val)
        except ValueError:
            raise SystemExit(f"{what}: expected ROLE=SECONDS, got {it!r}")
    return out


def main(argv=None, default_task: str = "basic") -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--providers", default="auto", help="auto | mock | comma list: anthropic,gemini,ollama,mock,mock2")
    ap.add_argument("--task", default=default_task, metavar="NAME[,NAME...]|all",
                    help="task(s) to build; available: " + ", ".join(["basic", *_RADIOMICS_NAMES]) +
                    f" (default {default_task})")
    ap.add_argument("--rounds", type=int, default=3, help="max review/rework rounds (default 3)")
    ap.add_argument("--runs", type=int, default=1, help="repeat the whole run N times (fresh sandboxes)")
    ap.add_argument("--json-out", default="", help="write per-run JSON (rounds, agents, hidden pass/total, baseline)")
    ap.add_argument("--sandbox", default="", help="sandbox dir (default: new mkdtemp, never deleted; one task, --runs 1 only)")
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--baseline-repair", type=int, default=0, help="extra self-repair rounds for the baseline (reported separately)")
    ap.add_argument("--retries", type=int, default=1, help="bounded retries per agent per provider (not for auth errors)")
    ap.add_argument("--inject-bad-key", action="append", default=[], metavar="ROLE",
                    help="give ROLE's primary provider a wrong key (e.g. worker-2, reviewer, all)")
    ap.add_argument("--inject-timeout", action="append", default=[], metavar="ROLE=SEC",
                    help="short timeout for ROLE's primary provider")
    ap.add_argument("--model", default="", help="model id for providers without a built-in default (e.g. ollama)")
    ap.add_argument("--log-file", default="", help="also write the redacted console output here")
    a = ap.parse_args(argv)
    names = list(available_tasks()) if a.task == "all" else [n for n in a.task.split(",") if n]
    tasks = [get_task(n) for n in names]
    if not tasks:
        raise SystemExit("--task: no task given")
    if a.model:
        os.environ["LOOM_MODEL"] = a.model
    extra = {} if a.providers == "mock" else secrets_env()
    providers = pick_providers(extra) if a.providers == "auto" else a.providers.split(",")
    secrets = list(extra.values())
    logf = open(a.log_file, "w", encoding="utf-8", buffering=1) if a.log_file else None

    def out(s):
        s = redact(s, secrets)
        print(s, flush=True)
        if logf:
            logf.write(s + "\n")

    try:
        import pytest  # noqa: F401 - the harness runs the hidden tests with this interpreter
    except ImportError:
        sys.exit(f"pytest is required by the hidden-test harness: run `{sys.executable} -m pip install pytest`")
    out(f"providers: {','.join(providers)} | keys found: {sorted(extra)}")
    allmock = all(is_mock(p) for p in providers)
    loom = "" if allmock else ensure_loom()
    lv = "n/a (mock path does not run loom)" if allmock else loom_version(loom)
    out(f"loom version: {lv}")
    inj = Inject(set(a.inject_bad_key), _kv(a.inject_timeout, "--inject-timeout"))
    if inj.bad_key or inj.timeout:
        out(f"failure injection (primary provider only): bad_key={sorted(inj.bad_key)} timeout={inj.timeout}")
    runs = []
    single = a.runs == 1 and len(tasks) == 1
    for t in tasks:
        for n in range(a.runs):
            out(f"--- task {t.NAME}: run {n + 1}/{a.runs} ---")
            res = build(providers, loom, extra, a.rounds, a.sandbox if single else "", not a.no_baseline,
                        out, inj, a.retries, baseline_repair=a.baseline_repair, task=t)
            runs.append(to_json(res, lv))
    if len(runs) > 1:
        out(f"runs ok: {sum(r['ok'] for r in runs)}/{len(runs)}")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            f.write(redact(json.dumps({"providers": providers, "runs": runs}, indent=1), secrets))
    if logf:
        logf.close()
    return 0 if all(r["ok"] for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
