"""
test_qradiomics_medimage_demos.py — End-to-end tests for the practical
`qradiomics` + `medimage` multi-agent demo workflows in `nexus_lite.demos`,
including `agy` and `claude` CLI normalization and live agent execution.
"""

import shutil
from pathlib import Path
import pytest

import nexus_lite as nl
from nexus_lite import cli, pty


@pytest.fixture(autouse=True)
def isolated_demo_env(tmp_path, monkeypatch):
    """Isolate workspace and socket paths for each demo test."""
    ws = tmp_path / "nexus_demo_ws"
    socks = tmp_path / "socks"
    ws.mkdir(parents=True, exist_ok=True)
    socks.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_DIR", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(socks))
    monkeypatch.setenv("NEXUS_DELEGATION_DEPTH", "0")

    nl.init(ws)
    yield ws


def test_claude_and_agy_normalization():
    """Verify command normalization for both `agy` and `claude` in pty and oneshot modes."""
    for agent_bin in ("agy", "claude"):
        assert pty.normalize_agent_cmd(agent_bin, mode="pty") == f"{agent_bin} --dangerously-skip-permissions"
        assert (
            pty.normalize_agent_cmd(f"{agent_bin} --dangerously-skip-permissions", mode="pty")
            == f"{agent_bin} --dangerously-skip-permissions"
        )
        oneshot = pty.normalize_agent_cmd(agent_bin, mode="oneshot", prompt="analyze dicom")
        assert f"{agent_bin} --dangerously-skip-permissions -p" in oneshot
        assert "analyze dicom" in oneshot


def test_demo1_phantom_shape_swarm(isolated_demo_env):
    """Verify 3-worker 3D lung nodule phantom swarm (spic6, AHSN, manifest)."""
    res = nl.run_phantom_shape_swarm(agent="python")
    assert len(res["swarm_results"]) == 3
    for t in res["swarm_results"]:
        assert t["status"] == "completed"
        assert t["rc"] == 0

    rows = res["spic6"]["rows"]
    assert len(rows) == 5
    by_name = {r["phantom"]: r for r in rows}
    assert by_name["soft_benign_nodule"]["spic_Na"] < by_name["soft_lung_cancer_spiculated"]["spic_Na"]
    assert by_name["soft_lung_cancer_spiculated"]["spic_Na"] >= 4
    assert by_name["soft_juxta_vascular_cancer"]["spic_Na_att"] >= 1

    # Verify FTS5 wiki indexing
    hits = nl.wiki_search("spic6")
    assert any(h["slug"] == "radiomics/spic6_phantoms" for h in hits)


def test_demo2_hipaa_privacy_pipeline(isolated_demo_env):
    """Verify 3-stage role pipeline (driver -> verifier -> refiner) for DICOM PHI removal."""
    res = nl.run_hipaa_privacy_pipeline(agent="python")
    stages = res["pipeline_result"]["stages"]
    assert [s["stage"] for s in stages] == ["driver", "verifier", "refiner"]
    assert all(s["status"] == "completed" for s in stages)

    audit = res["audit"]
    assert audit["verdict"] == "PASS"
    assert audit["phi_findings_before"] > 0
    assert audit["phi_findings_after"] == 0
    assert audit["pseudo_patient_id"].startswith("ANON-")
    assert audit["hu_and_geometry_identical"] is True

    wiki_doc = nl.wiki_read("compliance/dicom_privacy_audit")
    assert "CONFIRM" in wiki_doc


def test_demo3_multimodal_imaging_swarm(isolated_demo_env):
    """Verify 3-worker multimodal swarm (CT L3 body composition, CHAOS MRI steatosis, PET SUV)."""
    res = nl.run_multimodal_imaging_swarm(agent="python")
    assert len(res["swarm_results"]) == 3
    assert all(t["status"] == "completed" for t in res["swarm_results"])

    ct = res["ct_bodycomp"]
    assert ct["muscle_area_cm2"] > 50.0
    assert ct["sat_area_cm2"] > 50.0

    mri = res["mri_steatosis"]
    assert len(mri["subjects"]) == 4
    assert mri["subjects"][0]["steatosis_positive_gt_5pct"] is False
    assert mri["subjects"][-1]["steatosis_positive_gt_5pct"] is True

    pet = res["pet_suv"]
    assert pet["qiba_liver_validation_passed"] is True
    assert pet["gtv_metabolic_response_pct"] < 0.0


def test_demo4_lung1_audit_pipeline(isolated_demo_env):
    """Verify 3-stage Lung1 data-leakage and reproducibility audit pipeline."""
    res = nl.run_lung1_audit_pipeline(agent="python", n_patients=60)
    stages = res["pipeline_result"]["stages"]
    assert [s["stage"] for s in stages] == ["driver", "navigator", "verifier"]
    assert all(s["status"] == "completed" for s in stages)

    review = res["review"]
    assert review["status"] == "REVIEW_COMPLETE"
    issues = [f["issue"] for f in review["findings"]]
    assert "CRITICAL_TARGET_LEAKAGE" in issues
    assert "SMALL_HOLDOUT_VARIANCE" in issues
    assert "MULTIPLE_HYPOTHESIS_INFLATION" in issues

    m = review["metrics"]
    assert m["auc_with_survival_leak"] == 1.0
    assert 0.5 < m["auc_leak_free_cv"] < 0.99


def test_cli_demo_subcommand(isolated_demo_env, capsys):
    """Verify `nexus-lite demo` CLI subcommands execute cleanly."""
    rc = cli.main(["demo", "phantoms", "--agent", "python"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "soft_lung_cancer_spiculated" in out

    rc_all = cli.main(["demo", "all", "--agent", "python"])
    assert rc_all == 0
    out_all = capsys.readouterr().out
    assert "Completed all 4 qradiomics + medimage multi-agent demos" in out_all
    assert (isolated_demo_env / "dashboard.html").exists()


@pytest.mark.skipif(
    shutil.which("agy") is None and shutil.which("claude") is None,
    reason="Neither agy nor claude binary is installed",
)
def test_live_agy_or_claude_hipaa_demo_pipeline(isolated_demo_env):
    """Verify live `agy`/`claude` 3-stage role pipeline on the HIPAA DICOM de-identification demo."""
    res = nl.run_hipaa_privacy_pipeline(agent="auto")
    assert res["audit"]["verdict"] == "PASS"
    assert any(a in res["agent_used"] for a in ("agy", "claude"))
    wiki_doc = nl.wiki_read("compliance/dicom_privacy_audit")
    assert "Live Multi-Agent Role Pipeline" in wiki_doc


def test_gemini_drive_two_way_symlink_and_overwrite(tmp_path):
    """Verify changes in ~/.gemini propagate back to Drive via symlink and overwrite_local_changes works."""
    local_gemini = tmp_path / "home" / ".gemini"
    drive_gemini = tmp_path / "gdrive" / ".gemini"

    local_gemini.mkdir(parents=True)
    drive_gemini.mkdir(parents=True)
    (drive_gemini / "oauth.json").write_text("OLD_TOKEN", encoding="utf-8")
    (local_gemini / "oauth.json").write_text("UPDATED_LOCAL_TOKEN", encoding="utf-8")

    # 1. Explicit overwrite_local_changes=True updates Drive before symlinking
    nl.sync_dotdir_to_drive(local_gemini, drive_gemini, overwrite_local_changes=True)
    assert local_gemini.is_symlink()
    assert (drive_gemini / "oauth.json").read_text(encoding="utf-8") == "UPDATED_LOCAL_TOKEN"

    # 2. Any subsequent edit or new file inside ~/.gemini writes directly to Google Drive
    (local_gemini / "oauth.json").write_text("REFRESHED_IN_SESSION", encoding="utf-8")
    (local_gemini / "new_state.json").write_text("{\"active\": true}", encoding="utf-8")
    assert (drive_gemini / "oauth.json").read_text(encoding="utf-8") == "REFRESHED_IN_SESSION"
    assert (drive_gemini / "new_state.json").read_text(encoding="utf-8") == "{\"active\": true}"


def test_fake_colab_environment_and_drive_mount_simulation(tmp_path, monkeypatch):
    """Simulate a full Colab session (Drive mount + ~/.gemini sync + Demo 1 execution in Drive workspace)."""
    fake_home = tmp_path / "root"
    fake_home.mkdir(parents=True)
    fake_drive_root = tmp_path / "content" / "drive" / "MyDrive"
    fake_drive_gemini = fake_drive_root / ".gemini"
    fake_drive_workspace = fake_drive_root / "nexus-lite"
    fake_drive_gemini.mkdir(parents=True)
    (fake_drive_gemini / "credentials.json").write_text("{\"token\": \"colab_drive_token\"}", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("COLAB_RELEASE_TAG", "release-colab-ci-sim")
    monkeypatch.delenv("NEXUS_LITE_DIR", raising=False)
    monkeypatch.delenv("NEXUS_LITE_HOME", raising=False)

    env_info = nl.ensure_env(
        workspace=str(fake_drive_workspace),
        gemini_drive_path=str(fake_drive_gemini),
        start_bridge_server=False,
    )
    assert env_info["env"] == "colab"
    assert Path(env_info["workspace"]) == fake_drive_workspace.resolve()
    assert (fake_home / ".gemini").is_symlink()
    assert (fake_home / ".gemini" / "credentials.json").read_text(encoding="utf-8") == "{\"token\": \"colab_drive_token\"}"

    # Mutate ~/.gemini as if agy refreshed its auth token in Colab
    (fake_home / ".gemini" / "credentials.json").write_text("{\"token\": \"refreshed_in_colab\"}", encoding="utf-8")
    assert (fake_drive_gemini / "credentials.json").read_text(encoding="utf-8") == "{\"token\": \"refreshed_in_colab\"}"

    # Run Demo 1 inside the simulated Colab Drive workspace
    demo1 = nl.run_phantom_shape_swarm(agent="python")
    assert len(demo1["spic6"]["rows"]) == 5
    assert len(demo1["ahsn"]["summary"]) == 3


def test_qshape_loader_bypasses_broken_pydantic_init(isolated_demo_env, tmp_path, monkeypatch):
    """Simulate Colab's pydantic TypeError during qradiomics.__init__ import and verify Demo 1 still succeeds."""
    import importlib.util

    # Prepend a broken pydantic module on PYTHONPATH so `import qradiomics` raises TypeError
    broken_site = tmp_path / "broken_site"
    broken_site.mkdir(parents=True)
    (broken_site / "pydantic.py").write_text(
        "raise TypeError('Simulated Colab pydantic/typing_extensions mismatch')\n",
        encoding="utf-8",
    )

    # If qradiomics is not installed in the test environment (e.g. minimal CI or Colab base demo),
    # provide a synthetic qradiomics package whose __init__.py imports the broken pydantic while
    # qradiomics.shape remains importable via _load_qshape()'s package-init bypass.
    if importlib.util.find_spec("qradiomics") is None:
        qr_pkg = broken_site / "qradiomics"
        qr_shape = qr_pkg / "shape"
        qr_shape.mkdir(parents=True)
        (qr_pkg / "__init__.py").write_text("import pydantic\n", encoding="utf-8")
        (qr_shape / "__init__.py").write_text(
            "import numpy as np\n"
            "class AHSNConfig:\n"
            "    pass\n"
            "class _Phantom:\n"
            "    def __init__(self):\n"
            "        self.volume = np.zeros((16, 16, 16), dtype='float32')\n"
            "        self.mask = np.ones((16, 16, 16), dtype=bool)\n"
            "        self.attachment_mask = np.zeros((16, 16, 16), dtype=bool)\n"
            "class _SpicFeats:\n"
            "    def as_dict(self):\n"
            "        return {'Np': 31.0, 'Na': 1.0, 'Nl': 24.0, 'Na_att': 0.0, 's1': -0.3041, 's2': 0.073}\n"
            "def make(name, size=48):\n"
            "    return _Phantom()\n"
            "def spiculation_from_voxel(mask, spacing=(1.0, 1.0, 1.0), attachment_mask=None, n_param_iter=80):\n"
            "    return _SpicFeats(), None, None, None\n"
            "def ahsn(volume, mask=None, cfg=None):\n"
            "    return np.ones(180, dtype='float32') / 180.0\n"
            "def wall_eliminate(volume, cfg=None, area_threshold=40):\n"
            "    return volume, np.ones_like(volume, dtype=bool)\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("PYTHONPATH", str(broken_site))
    res = nl.run_phantom_shape_swarm(agent="python")
    assert "rows" in res["spic6"]
    assert len(res["spic6"]["rows"]) == 5
    assert res["spic6"]["engine"] == "qradiomics.shape.spiculation_from_voxel"
    assert "summary" in res["ahsn"]
    assert len(res["ahsn"]["summary"]) == 3
    assert res["ahsn"]["engine"] == "qradiomics.shape.ahsn"


def test_opencode_normalization_and_free_demo(isolated_demo_env, monkeypatch):
    """Verify `opencode` zero-signup model normalization and `run_opencode_free_demo` fallback & wiki indexing."""
    # Exercise candidate Path fallback when `opencode` is not on PATH (as on a fresh Colab VM)
    monkeypatch.setattr(pty.shutil, "which", lambda _: None)
    _ = pty.find_opencode_bin()

    pty_cmd = pty.normalize_agent_cmd("opencode", mode="pty")
    assert "-m opencode/big-pickle" in pty_cmd or "-m 'opencode/big-pickle'" in pty_cmd

    oneshot_default = pty.normalize_agent_cmd("opencode", mode="oneshot", prompt="verify spic6")
    assert "run --standalone --auto -m" in oneshot_default
    assert "opencode/big-pickle" in oneshot_default
    assert "verify spic6" in oneshot_default

    oneshot_model = pty.normalize_agent_cmd("opencode/glm-5.3-flash-free", mode="oneshot", prompt="audit lung1")
    assert "run --standalone --auto -m" in oneshot_model
    assert "opencode/glm-5.3-flash-free" in oneshot_model
    assert "audit lung1" in oneshot_model

    # Force deterministic fallback in unit test so CI doesn't depend on external network
    monkeypatch.setattr(pty, "find_opencode_bin", lambda: None)
    res = nl.run_opencode_free_demo()
    assert len(res["swarm"]) == 3
    assert all(r["status"] == "completed" for r in res["swarm"])
    assert "PASS" in res["pipeline"]["final_output"]
    wiki_doc = nl.wiki_read("opencode/zero_signup_multi_agent_demo")
    assert "Zero-Signup OpenCode Multi-Agent Demo" in wiki_doc
