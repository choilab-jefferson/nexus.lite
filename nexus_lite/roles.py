"""
nexus_lite.roles — Open-Core Kernel Phase 1 role stances and resolution.

Provides role-prompt stances ('driver', 'navigator', 'refiner', 'verifier', 'thinker', 'worker'),
agent-to-role mappings, real agent definitions, tier rankings, and role dispatch resolution.
"""

from __future__ import annotations

from typing import Optional

ROLE_PROMPTS: dict[str, str] = {
    "worker": (
        "You are a general-purpose executor. Complete the assigned task directly. "
        "Write code, make changes, return concrete results. Do not over-explain."
    ),
    "thinker": (
        "You are a planning specialist. Decompose the problem into actionable sub-tasks. "
        "Analyze requirements, identify risks, produce a structured plan. "
        "Do NOT implement — your output is the plan only."
    ),
    "driver": (
        "You are an implementation specialist. Make surgical, minimal code changes. "
        "Match existing style. Touch only what the task requires. "
        "No speculative features, no unnecessary refactoring."
    ),
    "navigator": (
        "You are a code reviewer. Identify logic errors, security issues, edge cases. "
        "Do NOT modify code. For each issue: file, line, problem, recommended fix."
    ),
    "refiner": (
        "You are the final decision maker. Issue ONE verdict:\n"
        "  CONFIRM — ready to ship.\n"
        "  MODIFY  — list specific changes needed.\n"
        "  ROLLBACK — approach fundamentally flawed, start over.\n"
        "State verdict on the first line."
    ),
    "verifier": (
        "You are an output validator. Respond ONLY with:\n"
        "  PASS — meets all success criteria.\n"
        "  FAIL — fixable issues found (list them).\n"
        "  HALT — unrecoverable state, escalate.\n"
        "First line must be exactly: PASS, FAIL, or HALT."
    ),
}

ROLE_FOR_AGENT: dict[str, str] = {
    "mini-worker": "worker",
    "mini-driver": "driver",
    "mini-navigator": "navigator",
    "mini-refiner": "refiner",
    "mini-verifier": "verifier",
    "mini-nexus": "thinker",
}

# The only two real, dispatchable agent identities in the 2-agent model.
REAL_AGENTS: frozenset[str] = frozenset({"mini-worker", "mini-nexus"})

# Default Claude tier per role (explicit model_id always wins).
ROLE_TIER: dict[str, str] = {
    "worker": "sonnet",
    "driver": "sonnet",
    "verifier": "sonnet",
    "navigator": "opus",
    "thinker": "opus",
    "refiner": "opus",
}

TIER_ORDER: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")


def tier_rank(model_or_tier: Optional[str]) -> int:
    """Return index in TIER_ORDER matching tier substring in lowercased model_or_tier, defaulting to 1 ('sonnet')."""
    if not model_or_tier:
        return 1
    low = model_or_tier.lower().strip()
    if low in TIER_ORDER:
        return TIER_ORDER.index(low)
    for idx, tier in enumerate(TIER_ORDER):
        if tier in low:
            return idx
    return 1


def tier_for_role(
    role: Optional[str],
    producer_model: Optional[str] = None,
    *,
    agent_name: Optional[str] = None,
) -> str:
    """
    Return the default tier for a role (or agent_name fallback).
    When role is 'verifier' and producer_model is provided, clamps to
    max(base_rank, min(tier_rank(producer_model), TIER_ORDER.index('opus'))),
    never auto-escalating to 'fable'.
    """
    eff_role = role or (ROLE_FOR_AGENT.get(agent_name or "") if agent_name else None) or "worker"
    base_tier = ROLE_TIER.get(eff_role, ROLE_TIER["worker"])
    if eff_role == "verifier" and producer_model:
        base_rank = tier_rank(base_tier)
        prod_rank = min(tier_rank(producer_model), TIER_ORDER.index("opus"))
        return TIER_ORDER[max(base_rank, prod_rank)]
    return base_tier


def get_role_prompt(role: str) -> str:
    """Return prompt text for a role stance, defaulting to worker."""
    return ROLE_PROMPTS.get(role, ROLE_PROMPTS["worker"])


def resolve_role_dispatch(
    agent_name: str,
    prompt: str = "",
    *,
    default_role: Optional[str] = None,
    skip_real_agents: bool = True,
) -> tuple[str, str, Optional[str]]:
    """
    Resolve logical or legacy agent name into (final_agent, prompt, role).

    - If agent_name is in REAL_AGENTS and skip_real_agents is True, returns (agent_name, prompt, None).
    - Otherwise maps agent_name via ROLE_FOR_AGENT (or default_role).
    - Returns (final_agent, role_wrapped_prompt, role).
    """
    if skip_real_agents and agent_name in REAL_AGENTS:
        role = None
    else:
        role = ROLE_FOR_AGENT.get(agent_name, default_role)

    if not role:
        return agent_name, prompt, None

    final_agent = "mini-nexus" if role == "thinker" else "mini-worker"
    role_text = get_role_prompt(role)
    if prompt:
        final_prompt = f"[ROLE: {role}]\n{role_text}\n\n[TASK]\n{prompt}"
    else:
        final_prompt = f"[ROLE: {role}]\n{role_text}"

    return final_agent, final_prompt, role
