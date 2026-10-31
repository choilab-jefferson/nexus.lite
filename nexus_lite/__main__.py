"""Allow `python -m nexus_lite` (same entrypoint as the `nexus-lite` console script)."""
import sys

from nexus_lite.cli import main

if __name__ == "__main__":
    sys.exit(main())
