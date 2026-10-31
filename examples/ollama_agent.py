#!/usr/bin/env python3
"""stdin -> local Ollama (GPU) -> stdout. Usable as a nexus-lite `cmd` agent.

Usage: echo "prompt" | python3 ollama_agent.py [--model qwen2.5:3b] [--system TEXT]
"""
import argparse, json, re, sys, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--model", default="gemma4:12b")
ap.add_argument("--system", default="")
ap.add_argument("--host", default="http://127.0.0.1:11434")
ap.add_argument("--max-tokens", type=int, default=600)
a = ap.parse_args()

body = {"model": a.model, "prompt": sys.stdin.read(), "system": a.system,
        "stream": False, "options": {"num_predict": a.max_tokens, "temperature": 0.3}}
req = urllib.request.Request(a.host + "/api/generate", json.dumps(body).encode(),
                             {"Content-Type": "application/json"})
try:
    out = json.load(urllib.request.urlopen(req, timeout=600))["response"]
    print(re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip())
except Exception as e:
    print(f"ollama_agent error: {e}", file=sys.stderr)
    sys.exit(1)
