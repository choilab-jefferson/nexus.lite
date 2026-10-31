#!/usr/bin/env python3
"""Thin wrapper: `python examples/loom_multi_agent.py [--providers mock]`."""
import sys
from nexus_lite.loom_multi_agent import main

sys.exit(main())
