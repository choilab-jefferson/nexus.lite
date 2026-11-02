"""Deprecated module path: moved to nexus_lite.demos.loom_multi_agent."""

import sys
import warnings
from importlib import import_module

warnings.warn(
    "nexus_lite.loom_multi_agent is deprecated; import nexus_lite.demos.loom_multi_agent instead",
    DeprecationWarning,
    stacklevel=2,
)
sys.modules[__name__] = import_module("nexus_lite.demos.loom_multi_agent")
