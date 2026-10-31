import json

from nexus_lite import loom_multi_agent as m


def test_pick_providers_falls_back_to_mock():
    assert m.pick_providers({}) == ["mock"]
    assert m.pick_providers({"GEMINI_API_KEY": "x"}) == ["gemini"]


def test_mock_keys_not_forwarded(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sekret")
    assert m.secrets_env() == {"ANTHROPIC_API_KEY": "sekret"}  # non-colab: env passthrough
    assert "sekret" not in json.dumps(m.pick_providers({}))


def test_notebook_has_no_outputs():
    nb = json.load(open("notebooks/loom_multi_agent_colab.ipynb"))
    assert all(not c.get("outputs") and c.get("execution_count") is None
               for c in nb["cells"] if c["cell_type"] == "code")
