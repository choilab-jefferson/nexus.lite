import pytest

_GUARD_ENV = (
    "NEXUS_DELEGATED_ROLE",
    "NEXUS_DELEGATION_DEPTH",
    "NEXUS_MAX_DELEGATION_DEPTH",
    "NEXUS_ALLOW_NESTED_MININEXUS",
    "NEXUS_SENSITIVITY",
    "NEXUS_ARTIFACTS_DIR",
)


@pytest.fixture(autouse=True)
def _hermetic_nexus_env(monkeypatch):
    """Keep the suite independent of an enclosing nexus worker shell."""
    for name in _GUARD_ENV:
        monkeypatch.delenv(name, raising=False)
