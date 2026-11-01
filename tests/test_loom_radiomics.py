"""Radiomics loom tasks: reference passes hidden tests, flawed modules fail exactly, mock e2e, no prompt leak."""
import os

import pytest

from nexus_lite import loom_multi_agent as m
from nexus_lite.loom_radiomics_tasks import TASKS

NAMES = ["radiomics-firstorder", "radiomics-texture", "radiomics-stats"]


def _quiet(*_a, **_k):
    pass


def _score(task, files, tmp_path):
    sb, hd = tmp_path / "sb", tmp_path / "hd"
    sb.mkdir(exist_ok=True)
    for f, src in files.items():
        (sb / f).write_text(src)
    m.write_hidden(str(hd), task)
    return m.run_hidden(str(sb), str(hd), task=task)


def test_registry_lists_tasks():
    assert set(NAMES) <= set(m.available_tasks())
    assert m.get_task("basic").FILES == m.FILES
    with pytest.raises(SystemExit):
        m.get_task("nope")


@pytest.mark.parametrize("name", NAMES)
def test_task_shape(name):
    t = TASKS[name]
    assert 3 <= len(t.FILES) <= 4 and set(t.MOCK_FILES) == set(t.FILES)
    assert [s["file"] for s in t.DEFAULT_SPLIT] == t.FILES
    assert t.HIDDEN_TOTAL >= 20 and set(t.HIDDEN_TESTS) == {"test_h_" + f for f in t.FILES}


@pytest.mark.parametrize("name", NAMES)
def test_good_passes_all_hidden(name, tmp_path):
    t = TASKS[name]
    r = _score(t, {f: p[1] for f, p in t.MOCK_FILES.items()}, tmp_path)
    assert r["failing"] == [] and r["passed"] == r["total"] == t.HIDDEN_TOTAL


@pytest.mark.parametrize("name", NAMES)
def test_flawed_fails_exactly_flawed_modules(name, tmp_path):
    t = TASKS[name]
    flawed = {f for f, (bad, good) in t.MOCK_FILES.items() if bad != good}
    assert len(flawed) == 2
    r = _score(t, {f: p[0] for f, p in t.MOCK_FILES.items()}, tmp_path)
    assert {mod for mod, _n, _msg in r["failing"]} == flawed
    assert m.blame(r, t.FILES, ) and set(m.blame(r, t.FILES)) == flawed


@pytest.mark.parametrize("name", NAMES)
def test_mock_end_to_end_and_no_prompt_leak(name, tmp_path, monkeypatch):
    t = TASKS[name]
    prompts = []
    real = m.mock_agent

    def spy(role, prompt, *a, **k):
        prompts.append(prompt)
        return real(role, prompt, *a, **k)
    monkeypatch.setattr(m, "mock_agent", spy)
    res = m.build(["mock"], "", {}, sandbox=str(tmp_path / "sb"), out=_quiet, backoff=0, task=t)
    assert res["ok"] and res["rounds"] > 1
    assert res["hidden"]["passed"] == res["hidden"]["total"] == t.HIDDEN_TOTAL
    assert prompts and any("Acceptance tests (hidden" in p for p in prompts)
    marker = "hidden-suite-marker-7f3a91"
    assert not any(marker in p or "def test_" in p for p in prompts)
    # no hidden test body fragment in any prompt
    frags = {line.strip() for src in t.HIDDEN_TESTS.values() for line in src.splitlines()
             if line.strip().startswith("assert ") and len(line.strip()) > 40}
    assert frags and not any(fr in p for fr in frags for p in prompts)
    assert not any(n.startswith("test_h_") for n in os.listdir(res["sandbox"]))
    assert t.SPEC in prompts[1] or any(t.SPEC in p for p in prompts)


def test_cli_main_runs_all_radiomics_tasks(tmp_path, capsys):
    from nexus_lite.loom_radiomics_tasks import DEFAULT_TASKS
    rc = m.main(["--providers", "mock", "--no-baseline", "--task", DEFAULT_TASKS,
                 "--json-out", str(tmp_path / "o.json")])
    assert rc == 0
    import json
    runs = json.load(open(tmp_path / "o.json"))["runs"]
    assert [r["task"] for r in runs] == NAMES and all(r["ok"] for r in runs)
    assert "radiomics-stats" in capsys.readouterr().out
