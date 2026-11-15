"""
nexus_lite.pty — the dependency-free PTY primitive: session server, clients, control API and screen reader.

Modules: :mod:`.protocol` (dtach 0.9 packets), :mod:`.server` (holds the PTY, serves the socket), :mod:`.client`
(interactive attach, programmatic :class:`~.client.Connection`), :mod:`.control` (spawn / send / keys / read / wait /
kill / discovery), :mod:`.screen` (stdlib VT text-screen reader), :mod:`.agents` (agent command normalization),
:mod:`.cli` (``python -m nexus_lite.pty serve|attach``). Specification: ``docs/specs/pty-package.md``.

Every name the former single-module ``nexus_lite.pty`` exported is re-exported here, so
``nexus_lite.pty.spawn(...)`` and friends keep working.
"""

from __future__ import annotations

import shutil  # noqa: F401  (``pty.shutil`` is patched by callers)

from . import agents, client, protocol, screen, server  # noqa: F401
from . import control  # noqa: F401
from .agents import *  # noqa: F401,F403
from .control import *  # noqa: F401,F403
from .control import (  # noqa: F401  (private names that callers and tests reach through the package)
    _PKT, _PUSH_MAX, _SOCK_DIR_CAPABLE, _find_dtach_pid, _is_master_argv, _on_drive_fuse, _packet, _pid_alive,
    _probe_connect, _read_pid_sidecar, _winch_packet,
)
from .protocol import *  # noqa: F401,F403
from .screen import Screen, render  # noqa: F401
