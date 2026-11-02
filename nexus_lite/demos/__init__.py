"""
nexus_lite.demos — demo and orchestration scenarios built on the nexus-lite kernel.

Everything here depends on the kernel (`nexus_lite.core`, `pty`, `memory`); the kernel never
imports this package. CLI entry point: `nexus-lite-demo`.

- `scenarios`      qradiomics/medimage demos (formerly `nexus_lite/demos.py`)
- `orchestration`  multi-tier swarm, A2A interaction, cooperative solver (formerly in `core`)
- `viz`            HTML5 dashboard
- `loom_*`         loom multi-agent example tasks
"""

from __future__ import annotations

from .orchestration import (
    run_agent_interaction,
    run_cooperative_problem_solver,
    run_multi_tier_swarm,
)
from .scenarios import (
    run_all_demos,
    run_hipaa_privacy_pipeline,
    run_lung1_audit_pipeline,
    run_multimodal_imaging_swarm,
    run_opencode_free_demo,
    run_phantom_shape_swarm,
)

__all__ = [
    "run_agent_interaction",
    "run_all_demos",
    "run_cooperative_problem_solver",
    "run_hipaa_privacy_pipeline",
    "run_lung1_audit_pipeline",
    "run_multi_tier_swarm",
    "run_multimodal_imaging_swarm",
    "run_opencode_free_demo",
    "run_phantom_shape_swarm",
]
