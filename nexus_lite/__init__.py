"""
nexus-lite: Open-Core Kernel for Nexus multi-agent primitives.

Eagerly loads the Open-Core Kernel modules (`core`, `pty`, `roles`, `protocol`)
and lazy-loads optional extras (`bridge`, `demos`, `viz`) on first attribute access (PEP 562).
"""

from __future__ import annotations

import importlib
from typing import Any

from . import core
from . import protocol
from . import pty
from . import roles
from . import stm
from .core import (
    ColabInitResult,
    bus_read,
    bus_send,
    chat,
    delegate,
    detect_env,
    ensure_colab,
    ensure_env,
    ensure_opencode,
    get_db,
    get_db_path,
    get_workspace,
    init,
    run_agent_interaction,
    run_cooperative_problem_solver,
    run_multi_tier_swarm,
    run_pipeline,
    run_swarm,
    stm_list,
    stm_pack,
    stm_read,
    stm_rebuild_index,
    stm_search,
    stm_write,
    sync_dotdir_to_drive,
    wiki_list,
    wiki_read,
    wiki_search,
    wiki_write,
)
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

__version__ = "0.1.4"

_LAZY_MODULES = {"bridge", "demos", "viz"}

_LAZY_SYMBOLS: dict[str, str] = {
    # bridge
    "RemoteColab": "bridge",
    "ensure_cloudflared": "bridge",
    "get_bridge_info": "bridge",
    "start_bridge": "bridge",
    "stop_bridge": "bridge",
    # demos
    "run_all_demos": "demos",
    "run_hipaa_privacy_pipeline": "demos",
    "run_lung1_audit_pipeline": "demos",
    "run_multimodal_imaging_swarm": "demos",
    "run_opencode_free_demo": "demos",
    "run_phantom_shape_swarm": "demos",
    # viz
    "dashboard": "viz",
    "render_dashboard_html": "viz",
    "watch": "viz",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    if name in _LAZY_SYMBOLS:
        mod_name = _LAZY_SYMBOLS[name]
        mod = importlib.import_module(f".{mod_name}", __name__)
        globals()[mod_name] = mod
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "pty",
    "core",
    "roles",
    "protocol",
    "stm",
    "viz",
    "bridge",
    "demos",
    "init",
    "get_workspace",
    "get_db",
    "get_db_path",
    "bus_send",
    "bus_read",
    "delegate",
    "run_swarm",
    "run_pipeline",
    "run_multi_tier_swarm",
    "run_agent_interaction",
    "run_cooperative_problem_solver",
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
    "detect_env",
    "ensure_env",
    "ensure_colab",
    "ColabInitResult",
    "ensure_opencode",
    "sync_dotdir_to_drive",
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
    "render_dashboard_html",
    "dashboard",
    "watch",
    "ensure_cloudflared",
    "start_bridge",
    "stop_bridge",
    "get_bridge_info",
    "RemoteColab",
    "run_phantom_shape_swarm",
    "run_hipaa_privacy_pipeline",
    "run_multimodal_imaging_swarm",
    "run_lung1_audit_pipeline",
    "run_opencode_free_demo",
    "run_all_demos",
]
