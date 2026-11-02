"""Deprecated module path: moved to nexus_lite.demos.loom_radiomics_tasks."""

import sys
import warnings
from importlib import import_module

warnings.warn(
    "nexus_lite.loom_radiomics_tasks is deprecated; import nexus_lite.demos.loom_radiomics_tasks instead",
    DeprecationWarning,
    stacklevel=2,
)
sys.modules[__name__] = import_module("nexus_lite.demos.loom_radiomics_tasks")
