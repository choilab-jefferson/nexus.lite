import pytest

from nexus_lite import guard
from nexus_lite.guard import leaf_guard_error, current_delegation_depth, max_delegation_depth

ENVS = (guard.DELEGATION_DEPTH_ENV, guard.MAX_DELEGATION_DEPTH_ENV,
        guard.DELEGATED_ROLE_ENV, guard.ALLOW_NESTED_MININEXUS_ENV)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for e in ENVS:
        monkeypatch.delenv(e, raising=False)


def test_defaults_allow():
    assert current_delegation_depth() == 0
    assert max_delegation_depth() == 2
    assert leaf_guard_error("claude") is None


def test_depth_default_limit(monkeypatch):
    monkeypatch.setenv(guard.DELEGATION_DEPTH_ENV, "1")
    assert leaf_guard_error("x") is None
    monkeypatch.setenv(guard.DELEGATION_DEPTH_ENV, "2")
    assert "NEXUS_DELEGATION_DEPTH exceeded" in leaf_guard_error("x")


def test_max_override(monkeypatch):
    monkeypatch.setenv(guard.DELEGATION_DEPTH_ENV, "2")
    monkeypatch.setenv(guard.MAX_DELEGATION_DEPTH_ENV, "3")
    assert leaf_guard_error("x") is None
    monkeypatch.setenv(guard.MAX_DELEGATION_DEPTH_ENV, "bad")
    assert max_delegation_depth() == 2
    assert leaf_guard_error("x")


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5"])
def test_unparseable_depth(monkeypatch, raw):
    monkeypatch.setenv(guard.DELEGATION_DEPTH_ENV, raw)
    assert current_delegation_depth() is None
    assert "unknown" in leaf_guard_error("x")


def test_worker_role(monkeypatch):
    monkeypatch.setenv(guard.DELEGATED_ROLE_ENV, "worker")
    assert "leaf-executor" in leaf_guard_error("claude")
    assert leaf_guard_error("mini-nexus") is None
    monkeypatch.setenv(guard.DELEGATION_DEPTH_ENV, "2")
    assert leaf_guard_error("mini-nexus")  # depth still bounds


def test_thinker_nested(monkeypatch):
    monkeypatch.setenv(guard.DELEGATED_ROLE_ENV, "thinker")
    assert leaf_guard_error("mini-worker") is None
    assert "nested orchestration" in leaf_guard_error("mini-nexus")
    assert "nested orchestration" in leaf_guard_error("x", nested_orchestrator=True)
    monkeypatch.setenv(guard.ALLOW_NESTED_MININEXUS_ENV, "1")
    assert leaf_guard_error("mini-nexus") is None


def test_rule_order(monkeypatch):
    monkeypatch.setenv(guard.DELEGATED_ROLE_ENV, "worker")
    monkeypatch.setenv(guard.DELEGATION_DEPTH_ENV, "zzz")
    assert "leaf-executor" in leaf_guard_error("claude")
