#!/usr/bin/env python3
"""Thin wrapper: `python examples/loom_multi_agent.py [--providers mock] [--json-out out.json]`.

Run from a checkout this prefers the checkout over any older nexus-lite installed in
site-packages (no PYTHONPATH needed). Outside a checkout it needs a recent install.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.isfile(os.path.join(_ROOT, "nexus_lite", "loom_multi_agent.py")):
    sys.path.insert(0, _ROOT)  # checkout wins over an older installed copy

try:
    from nexus_lite.loom_multi_agent import main
except ImportError as e:
    sys.exit("nexus_lite.loom_multi_agent is missing (%s): the installed nexus-lite is too old.\n"
             "Run `pip install -e .` from a checkout of nexus.lite, or "
             "`pip install -U git+https://github.com/choilab-jefferson/nexus.lite.git`." % e)

sys.exit(main())
