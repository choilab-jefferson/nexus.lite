import sys

import pytest

from nexus_lite import core
from nexus_lite.demos import orchestration
from nexus_lite.phi import phi_gate_error


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_SENSITIVITY", raising=False)
    monkeypatch.setenv("NEXUS_LITE_DIR", str(tmp_path / "ws"))
    for e in ("NEXUS_DELEGATION_DEPTH", "NEXUS_MAX_DELEGATION_DEPTH", "NEXUS_DELEGATED_ROLE"):
        monkeypatch.delenv(e, raising=False)


@pytest.mark.parametrize("cmd", ["claude", "agy -p", "opencode/big-pickle", "ollama launch claude", "ollama run 'x"])
def test_unset_passes_everything(cmd):
    assert phi_gate_error(cmd) is None


@pytest.mark.parametrize("val", ["", "PHI", "none"])
def test_other_values_pass(monkeypatch, val):
    monkeypatch.setenv("NEXUS_SENSITIVITY", val)
    assert phi_gate_error("claude") is None


@pytest.mark.parametrize("cmd", ["claude", "/usr/bin/agy -p", "opencode/big-pickle", "ollama launch claude --model x",
                                 "ollama run 'unterminated", "", "curl http://x"])
def test_phi_refuses(monkeypatch, cmd):
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    assert phi_gate_error(cmd)


@pytest.mark.parametrize("cmd", ["ollama run x", "/usr/local/bin/ollama run llama3.2", "bash -c 'echo hi'", "sh -c true",
                                 "python3 -c 1", sys.executable + " -c 1", [sys.executable, "-c", "1"]])
def test_phi_allows_local(monkeypatch, cmd):
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    assert phi_gate_error(cmd) is None


def test_delegate_refuses_cloud_under_phi(monkeypatch):
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    with pytest.raises(PermissionError, match="PHI gate"):
        core.delegate("claude", "hi")


def test_delegate_forces_fallback_none(monkeypatch):
    monkeypatch.setenv("NEXUS_SENSITIVITY", "phi")
    ran = []
    real = core.subprocess.Popen

    def spy(cmd, *a, **kw):
        ran.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(core.subprocess, "Popen", spy)
    res = core.delegate("bash -c 'echo quota exceeded; exit 1'", "p", fallback_cmd="claude", timeout=20)
    assert res["rc"] != 0
    assert len(ran) == 1 and "claude" not in str(ran[0]).replace("quota", "")


def test_multi_tier_phi_no_cloud_cli(monkeypatch):
    seen = []

    class FakeProc:
        returncode = 0

        def communicate(self, input=None, timeout=None):
            return "ok", ""

    def fake_popen(cmd, *a, **kw):
        seen.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return FakeProc()

    monkeypatch.setattr(core.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(core.shutil, "which", lambda *_: None)
    res = orchestration.run_multi_tier_swarm("goal", phi=True)
    assert res["phi"] is True
    assert seen and all(c.startswith("ollama run ") for c in seen)
    assert not any(x in c for c in seen for x in ("opencode", "claude", "agy"))
    import os
    assert "NEXUS_SENSITIVITY" not in os.environ  # restored


def test_multi_tier_phi_explicit_cloud_cmd_refused():
    with pytest.raises(PermissionError, match="PHI gate"):
        orchestration.run_multi_tier_swarm("g", phi=True, tier0_tasks=[{"task_id": "t0", "cmd": "claude"}])
