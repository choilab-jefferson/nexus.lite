"""
nexus-lite: Open-Core Kernel for Nexus multi-agent primitives.

Eagerly loads only the light modules (`pty`, `roles`, `protocol`); `core` and the memory tiers
(`nexus_lite.memory.{stm,mtm,ltm}`) load on first attribute access (PEP 562), so
`import nexus_lite.memory.ltm` pulls in no other tier.

Demos/orchestration scenarios (`nexus_lite.demos`) and Colab helpers (`nexus_lite.colab`) are
outside the kernel; their old top-level names resolve lazily with a DeprecationWarning.
"""

from __future__ import annotations

import importlib
import warnings
from importlib import metadata as _metadata
from typing import Any

from . import protocol
from . import pty
from . import roles
from .protocol import (
    POINTER_MAX_CHARS,
    format_pointer_line,
    parse_pointer_line,
    pointer_parts,
    sanitize_pointer_token,
    truncate_words,
)
from .pty import (
    KEY_MAP,
    KEY_NAMES,
    key_to_bytes,
    keys_to_bytes,
)
from .roles import (
    REAL_AGENTS,
    ROLE_FOR_AGENT,
    ROLE_PROMPTS,
    ROLE_TIER,
    TIER_ORDER,
    get_role_prompt,
    resolve_role_dispatch,
    tier_for_role,
    tier_rank,
)

try:  # single source of truth: pyproject.toml via installed metadata
    __version__ = _metadata.version("nexus-lite")
except _metadata.PackageNotFoundError:  # bare source tree / shipped bundle: read pyproject.toml if adjacent
    import re as _re
    from pathlib import Path as _Path

    try:
        _m = _re.search(r'(?m)^version\s*=\s*"([^"]+)"',
                        (_Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError:
        _m = None
    __version__ = _m.group(1) if _m else "0+unknown"

_LAZY_MODULES = {"compute", "core", "demos"}

_LAZY_SYMBOLS: dict[str, str] = {
    # compute (zero-preinstall remote execution on Turing & Colab)
    "compute_run": "compute",
    "pack_lite_bundle": "compute",
    "pack_self_contained_script": "compute",
    "run_local_sandbox": "compute",
    "run_on_colab": "compute",
    "run_on_turing": "compute",
    "turing_job_status": "compute",
    # core
    "chat": "core",
    "delegate": "core",
    "ensure_opencode": "core",
    "run_pipeline": "core",
    "run_swarm": "core",
    # _db / memory tiers
    "get_workspace": "_db",
    "init": "_db",
    "bus_read": "memory.mtm",
    "bus_send": "memory.mtm",
    "wiki_list": "memory.ltm",
    "wiki_read": "memory.ltm",
    "wiki_search": "memory.ltm",
    "wiki_write": "memory.ltm",
    "stm_list": "memory.stm",
    "stm_pack": "memory.stm",
    "stm_read": "memory.stm",
    "stm_rebuild_index": "memory.stm",
    "stm_search": "memory.stm",
    "stm_write": "memory.stm",
}

# Deprecated top-level names: resolved lazily with a DeprecationWarning.
_DEPRECATED_SYMBOLS: dict[str, str] = {
    "get_db": "_db",
    "get_db_path": "_db",
    **{n: "demos.scenarios" for n in (
        "run_all_demos", "run_hipaa_privacy_pipeline", "run_lung1_audit_pipeline",
        "run_multimodal_imaging_swarm", "run_opencode_free_demo", "run_phantom_shape_swarm")},
    **{n: "demos.orchestration" for n in (
        "run_agent_interaction", "run_cooperative_problem_solver", "run_multi_tier_swarm")},
    **{n: "demos.viz" for n in ("dashboard", "render_dashboard_html", "watch")},
    **{n: "colab.env" for n in (
        "ColabInitResult", "detect_env", "ensure_colab", "ensure_env", "sync_dotdir_to_drive")},
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    if name in _LAZY_SYMBOLS:
        mod_name = _LAZY_SYMBOLS[name]
        mod = importlib.import_module(f".{mod_name}", __name__)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    if name in _DEPRECATED_SYMBOLS:
        mod_name = _DEPRECATED_SYMBOLS[name]
        warnings.warn(
            f"nexus_lite.{name} is deprecated; use nexus_lite.{mod_name}.{name}",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(importlib.import_module(f".{mod_name}", __name__), name)
    if name == "stm":  # deprecated module alias, see nexus_lite/stm.py
        warnings.warn(
            "nexus_lite.stm is deprecated; import nexus_lite.memory.stm instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return importlib.import_module(".memory.stm", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "pty",
    "compute",
    "core",
    "roles",
    "protocol",
    "compute_run",
    "run_on_turing",
    "run_on_colab",
    "run_local_sandbox",
    "pack_lite_bundle",
    "pack_self_contained_script",
    "turing_job_status",
    "init",
    "get_workspace",
    "bus_send",
    "bus_read",
    "delegate",
    "run_swarm",
    "run_pipeline",
    "stm_write",
    "stm_read",
    "stm_list",
    "stm_search",
    "stm_pack",
    "stm_rebuild_index",
    "wiki_write",
    "wiki_read",
    "wiki_search",
    "wiki_list",
    "chat",
    "ensure_opencode",
    "POINTER_MAX_CHARS",
    "sanitize_pointer_token",
    "truncate_words",
    "format_pointer_line",
    "pointer_parts",
    "parse_pointer_line",
    "KEY_NAMES",
    "KEY_MAP",
    "key_to_bytes",
    "keys_to_bytes",
    "ROLE_PROMPTS",
    "ROLE_FOR_AGENT",
    "REAL_AGENTS",
    "ROLE_TIER",
    "TIER_ORDER",
    "tier_rank",
    "tier_for_role",
    "resolve_role_dispatch",
    "get_role_prompt",
]
