#!/usr/bin/env python3
"""Thin wrapper: build all three radiomics tasks with the loom multi-agent team (default `--task` = all three).

    python examples/loom_radiomics.py --providers mock                    # offline, scripted
    python examples/loom_radiomics.py --task radiomics-stats --providers anthropic,gemini

Run from a checkout this prefers the checkout over any older nexus-lite installed in site-packages.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isfile(os.path.join(_ROOT, "nexus_lite", "demos", "loom_radiomics_tasks.py")):
    sys.path.insert(0, _ROOT)  # checkout wins over an older installed copy

try:
    from nexus_lite.demos.loom_multi_agent import main
    from nexus_lite.demos.loom_radiomics_tasks import DEFAULT_TASKS
except ImportError as e:
    sys.exit("nexus_lite.demos.loom_radiomics_tasks is missing (%s): the installed nexus-lite is too old.\n"
             "Run `pip install -e .` from a checkout of nexus.lite." % e)

sys.exit(main(default_task=DEFAULT_TASKS))
