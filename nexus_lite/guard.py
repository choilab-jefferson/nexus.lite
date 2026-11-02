"""Delegation depth / leaf guard primitive.

lite owns this primitive; nexus.code imports it and only extends it. Stdlib only, no daemon. nexus.code adds its own bits on top:
the API single-call exemption (those providers are tools, not agents) and
``resolve_role_dispatch`` for mapping a target to a role. Code calls
``leaf_guard_error`` after its own exemption check.

Backstop against runaway model recursion, not a security boundary: the stamps
are plain env vars.
"""
import os
from typing import Optional

DELEGATION_DEPTH_ENV = "NEXUS_DELEGATION_DEPTH"
MAX_DELEGATION_DEPTH_ENV = "NEXUS_MAX_DELEGATION_DEPTH"
DELEGATED_ROLE_ENV = "NEXUS_DELEGATED_ROLE"
ALLOW_NESTED_MININEXUS_ENV = "NEXUS_ALLOW_NESTED_MININEXUS"
DEFAULT_MAX_DELEGATION_DEPTH = 2


def max_delegation_depth() -> int:
    """``NEXUS_MAX_DELEGATION_DEPTH`` (default 2; unparseable/negative -> default)."""
    try:
        val = int(os.environ.get(MAX_DELEGATION_DEPTH_ENV, ""))
    except ValueError:
        return DEFAULT_MAX_DELEGATION_DEPTH
    return val if val >= 0 else DEFAULT_MAX_DELEGATION_DEPTH


def current_delegation_depth() -> Optional[int]:
    """This process's depth: 0 when unset/empty, None when unparseable/negative."""
    raw = os.environ.get(DELEGATION_DEPTH_ENV)
    if raw is None or raw.strip() == "":
        return 0
    try:
        val = int(raw)
    except ValueError:
        return None
    return val if val >= 0 else None


def leaf_guard_error(target: str = "", *, nested_orchestrator: bool = False) -> Optional[str]:
    """Refusal text when the CURRENT process may not dispatch ``target``, else None.

    Rules, in order:
    1. ``NEXUS_DELEGATED_ROLE=worker`` may dispatch nothing except 'mini-nexus'; rule 4 still bounds that.
    2. ``NEXUS_DELEGATED_ROLE=thinker`` (a mini-nexus) may not dispatch a nested
       orchestrator (target 'mini-nexus' or ``nested_orchestrator=True``) unless
       ``NEXUS_ALLOW_NESTED_MININEXUS=1``.
    3. An unparseable depth stamp is refused (fail closed).
    4. No dispatch once depth >= ``NEXUS_MAX_DELEGATION_DEPTH`` (default 2).
    """
    role = os.environ.get(DELEGATED_ROLE_ENV)
    if role == "worker" and target != "mini-nexus":
        return (
            "Error: recursive delegation forbidden: this session is a leaf-executor "
            f"worker ({DELEGATED_ROLE_ENV}=worker) and may not dispatch "
            f"'{target}'."
        )
    if (role == "thinker" and (nested_orchestrator or target == "mini-nexus")
            and os.environ.get(ALLOW_NESTED_MININEXUS_ENV) != "1"):
        return (
            "Error: nested orchestration forbidden: this session is a mini-nexus "
            f"({DELEGATED_ROLE_ENV}=thinker) and may not dispatch another "
            f"orchestrator '{target}'. Opt in with "
            f"{ALLOW_NESTED_MININEXUS_ENV}=1."
        )
    depth, limit = current_delegation_depth(), max_delegation_depth()
    if depth is None:
        return (
            f"Error: delegation depth unknown: {DELEGATION_DEPTH_ENV}="
            f"{os.environ.get(DELEGATION_DEPTH_ENV)!r} is not a non-negative "
            f"integer, so '{target}' is refused."
        )
    if depth >= limit:
        return (
            f"{DELEGATION_DEPTH_ENV} exceeded (current depth {depth} >= max {limit}): "
            f"may not dispatch '{target}'. Raise "
            f"{MAX_DELEGATION_DEPTH_ENV} to allow deeper nesting."
        )
    return None
