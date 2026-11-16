import os
import sys
import tempfile

import pytest


def pytest_configure(config):
    """macOS: pytest's default temp root (/private/var/folders/.../pytest-of-USER/) is so long that unix sockets the
    tests create under tmp_path exceed sun_path (104 bytes) and fail with "socket path too long". Linux CI has short
    paths. Without an explicit --basetemp, use a fresh short root under /tmp on macOS (one per run, so parallel runs
    never share it)."""
    if sys.platform == "darwin" and not config.option.basetemp:
        config.option.basetemp = tempfile.mkdtemp(prefix="nxl-", dir="/tmp")

_GUARD_ENV = (
    "NEXUS_DELEGATED_ROLE",
    "NEXUS_DELEGATION_DEPTH",
    "NEXUS_MAX_DELEGATION_DEPTH",
    "NEXUS_ALLOW_NESTED_MININEXUS",
    "NEXUS_SENSITIVITY",
    "NEXUS_ARTIFACTS_DIR",
    "NEXUS_BUS_DIR",
    "NEXUS_BUS_RUN_DIR",
    "NEXUS_LITE_MEM_MAX",
    "NEXUS_LITE_MEM_ENFORCER",
    "NEXUS_LITE_MEM_POLL",
    "XRUN_MEM_ENFORCER",
    "XRUN_MEM_POLL",
    "XRUN_ROOTS_INDEX",
    "RUN_DIR",
)


@pytest.fixture(autouse=True)
def _hermetic_nexus_env(monkeypatch, tmp_path_factory):
    """Keep the suite independent of an enclosing nexus worker shell (and keep `job start` out of the real roots index)."""
    for name in _GUARD_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_LITE_ROOTS_INDEX", str(tmp_path_factory.mktemp("rootsidx") / "roots"))
