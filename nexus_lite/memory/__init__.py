"""
nexus_lite.memory — the three memory tiers, one module each.

- `stm`: file-backed `stm/1` nodes (short-term memory; ids, schema, pack/search)
- `mtm`: file/SQLite message bus with inbox pointer delivery (mid-term memory)
- `ltm`: markdown wiki + SQLite FTS5 (long-term memory)

Importing this package imports none of them; use `nexus_lite.memory.<tier>` (or attribute
access, which imports lazily). Tiers do not import each other; shared workspace/SQLite
helpers live in `nexus_lite._db`.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["stm", "mtm", "ltm"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
