"""``python -m nexus_lite.pty serve|attach ...`` (see :mod:`nexus_lite.pty.cli`)."""

import sys

from .cli import main

sys.exit(main())
