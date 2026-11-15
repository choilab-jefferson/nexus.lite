"""
nexus_lite.ptyd — compatibility shim for the pre-package PTY server module (kept for one release).

The implementation moved into the :mod:`nexus_lite.pty` package (``pty.protocol``, ``pty.server``, ``pty.client``,
``pty.cli``). This module re-exports the old names and keeps ``python -m nexus_lite.ptyd [-r m] SOCK -- CMD...`` and
``python -m nexus_lite.ptyd -a SOCK`` working; new code uses ``python -m nexus_lite.pty serve|attach``.
"""

from __future__ import annotations

import sys

from .pty.cli import USAGE as _USAGE, main, parse_detach_char  # noqa: F401
from .pty.client import attach  # noqa: F401
from .pty.protocol import (  # noqa: F401
    DEFAULT_DETACH_CHAR, MSG_ATTACH, MSG_DETACH, MSG_PUSH, MSG_REDRAW, MSG_WINCH, PKT, PKT_SIZE, PUSH_MAX,
    REDRAW_CTRL_L, REDRAW_METHODS, REDRAW_NONE, REDRAW_UNSPEC, REDRAW_WINCH, packet, winsize_bytes,
)
from .pty.server import create, serve  # noqa: F401

MODULE = "nexus_lite.ptyd"  # argv token that marks a server / client process of this module

if __name__ == "__main__":
    sys.exit(main())
