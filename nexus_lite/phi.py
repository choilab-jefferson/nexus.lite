"""Minimal opt-in PHI gate: with NEXUS_SENSITIVITY=phi, only local runners may be dispatched.

Stateless: reads one env var, no store, no endpoint probe. Any value other than the
exact string ``phi`` leaves behaviour unchanged. A persistent classification store and
endpoint resolution are downstream extensions, not part of nexus.lite.
"""
from __future__ import annotations

import os
import shlex
import sys
from typing import Optional, Sequence, Union

_LOCAL_SHELLS = {"sh", "bash", "zsh", "dash"}
_PYTHONS = {"python", "python3"}


def phi_gate_error(agent_cmd: Union[str, Sequence[str]]) -> Optional[str]:
    """Return a refusal message if ``agent_cmd`` is not local under PHI, else None."""
    if os.environ.get("NEXUS_SENSITIVITY") != "phi":
        return None
    try:
        argv = list(agent_cmd) if not isinstance(agent_cmd, str) else shlex.split(agent_cmd)
    except ValueError as exc:
        return f"PHI gate: unparseable agent command refused ({exc})"
    if not argv:
        return "PHI gate: empty agent command refused"
    name = os.path.basename(str(argv[0]))
    if name == "ollama":
        if "launch" in argv[1:]:
            return "PHI gate: 'ollama launch' wraps a cloud-capable CLI; refused"
        return None
    if name in _LOCAL_SHELLS or name in _PYTHONS or name == os.path.basename(sys.executable):
        return None
    return f"PHI gate: '{name}' is not a local runner under NEXUS_SENSITIVITY=phi; refused"
