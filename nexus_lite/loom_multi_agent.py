"""Colab-ready multi-agent run on loom: orchestrator -> workers -> reviewer.

Vendored from nexus.loom `examples/multi_agent.py` (branch feat/multi-agent-example,
nexus.loom MR !22; same authors, same license) so lite does not depend on an
unmerged branch. Loom has no sub-agent tool, so each role is one
`loom -p ... --json` subprocess driven from here.

    python -m nexus_lite.loom_multi_agent                  # auto: keys -> real, else mock
    python -m nexus_lite.loom_multi_agent --providers mock

Stdlib only. API keys come ONLY from Colab Secrets (google.colab.userdata) or,
outside Colab, from the existing environment. They go to the loom child env and are
never printed, logged or written. Agents run in an empty temp workspace.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

LOOM_TAG = os.environ.get("LOOM_TAG", "v0.1.1")
LOOM_REL = "https://github.com/choilab-jefferson/nexus.loom/releases/download"
LOOM_SRC = "https://github.com/choilab-jefferson/nexus.loom"
ASSET = "loom-linux-amd64"
KEYS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
MODELS = {"anthropic": "claude-haiku-5-5", "gemini": "gemini-3.8-flash"}
FALLBACK_SPLIT = ["Explain what a goroutine is.", "Explain what a channel is."]


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


def agent(loom, role, provider, prompt, ws, extra_env):
    """Run one loom agent; return (ok, text)."""
    cmd = [loom, "-p", prompt, "--provider", provider, "--workspace", ws, "--json",
           "--no-agents-md", "--no-stm", "--max-turns", "2", "--max-total-tokens", "6000"]
    if provider in MODELS:
        cmd += ["--model", MODELS[provider]]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update(extra_env)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    try:
        r = json.loads(p.stdout)
        text, ok = r.get("output", "").strip(), p.returncode == 0
        used = r.get("usage", {}).get("total_tokens", "?")
    except ValueError:
        text, ok, used = "no output (exit %d)" % p.returncode, False, "?"  # stderr not echoed: may be provider text
    print(f"[{role:<12}] provider={provider:<9} ok={ok} tokens={used}")
    return ok, text


def run(providers, task, loom, extra_env):
    with tempfile.TemporaryDirectory() as ws:
        ok, text = agent(loom, "orchestrator", providers[0],
                         f'Split this task into 2 or 3 short independent subtasks. Reply with ONLY a JSON list of strings. Task: {task}',
                         ws, extra_env)
        try:
            subtasks = json.loads(text[text.index("["): text.rindex("]") + 1])[:3]
            assert subtasks and all(isinstance(s, str) for s in subtasks)
        except (ValueError, AssertionError):
            print("orchestrator reply not a JSON list; using fixed split")
            subtasks = FALLBACK_SPLIT
        results, live = [], list(providers)
        for i, sub in enumerate(subtasks):
            while live:
                prov = live[i % len(live)]
                ok, out = agent(loom, f"worker-{i + 1}", prov, f"{sub} Answer in at most 2 sentences.", ws, extra_env)
                if ok:
                    results.append((sub, out))
                    break
                live.remove(prov)
            else:
                results.append((sub, "(no provider available)"))
        body = "\n".join(f"- {s}: {o}" for s, o in results)
        ok, final = agent(loom, "reviewer", live[0] if live else providers[0],
                          f"Merge these worker results into one coherent answer of at most 3 sentences:\n{body}",
                          ws, extra_env)
    print("\n--- final ---\n" + final)
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="auto", help="auto | mock | comma list of anthropic,gemini")
    ap.add_argument("--task", default="Give a two-sentence overview of concurrency in Go.")
    a = ap.parse_args(argv)
    extra = {} if a.providers == "mock" else secrets_env()
    providers = pick_providers(extra) if a.providers == "auto" else a.providers.split(",")
    print("providers:", ",".join(providers), "| keys found:", sorted(k for k in extra))
    loom = ensure_loom()
    return run(providers, a.task, loom, extra)


if __name__ == "__main__":
    sys.exit(main())
