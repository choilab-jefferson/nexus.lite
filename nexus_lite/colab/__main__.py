"""CLI entrypoint when executed as `python -m nexus_lite.colab`."""

from __future__ import annotations

import sys
from .tunnel import main

if __name__ == "__main__":
    sys.exit(main())
