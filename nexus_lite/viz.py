"""Deprecated module path: moved to nexus_lite.demos.viz."""

import sys
import warnings
from importlib import import_module

warnings.warn(
    "nexus_lite.viz is deprecated; import nexus_lite.demos.viz instead",
    DeprecationWarning,
    stacklevel=2,
)
sys.modules[__name__] = import_module("nexus_lite.demos.viz")
