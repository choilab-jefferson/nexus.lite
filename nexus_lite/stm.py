"""Deprecated import path: use `nexus_lite.memory.stm` (this alias is removed after one release)."""

import sys
import warnings

from .memory import stm as _stm

warnings.warn(
    "nexus_lite.stm is deprecated; import nexus_lite.memory.stm instead",
    DeprecationWarning,
    stacklevel=2,
)
sys.modules[__name__] = _stm
