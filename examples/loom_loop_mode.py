#!/usr/bin/env python3
"""loom loop mode over the nexus file bus: one long-lived `loom serve`, several turns, no notebook.

    LOOM_BIN=/path/to/loom python examples/loom_loop_mode.py            # offline `mock` provider
    python examples/loom_loop_mode.py --provider anthropic --model MODEL # real model (key from the environment)

Needs a loom that reports `loop_mode` in `loom capabilities --json`; otherwise it says so and exits 0.
Run from a checkout this prefers the checkout over an older nexus-lite installed in site-packages.
"""
import argparse
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isfile(os.path.join(_ROOT, "nexus_lite", "loom.py")):
    sys.path.insert(0, _ROOT)  # checkout wins over an older installed copy

from nexus_lite.loom import LoomAdapter, LoomLoopError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--provider", default="mock")
    ap.add_argument("--model")
    ap.add_argument("--bus-dir", help="bus directory (default: a temp dir)")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="loom-loop-") as tmp:
        bus = args.bus_dir or os.path.join(tmp, "bus")
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        loop = LoomAdapter().serve(bus, "demo", provider=args.provider, model=args.model, workspace=ws,
                                   max_turns=10, max_wall=300, idle_timeout=120)
        try:
            with loop:
                print("session live; bus:", bus)
                t1 = loop.task("Summarise what a persistent loom context is in one sentence.", timeout=120)
                print(f"turn 1: status={t1.status} exit={t1.exit_code} body={t1.body[:80]!r}")
                loop.inject("Remember: the project codename is BLUEBIRD.")  # applied before the next turn
                t2 = loop.task("What is the project codename?", timeout=120)
                print(f"turn 2: status={t2.status} exit={t2.exit_code} trace={t2.trace_path}")
                ctx = loop.extract("context", timeout=60)
                print(f"extracted {ctx.schema}: {len(ctx.obj.get('messages', []))} messages -> {ctx.path}")
                print("stop exit code:", loop.stop("demo done"))
        except LoomLoopError as e:
            print(f"loop mode unavailable: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
