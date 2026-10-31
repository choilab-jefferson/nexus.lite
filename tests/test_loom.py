import json
import os
import stat
import sys

import pytest

from nexus_lite import loom
from nexus_lite.loom import LoomAdapter, probe

FAKE = r'''#!{py}
import json, os, sys, time
a = sys.argv[1:]
if a[:1] == ["capabilities"]:
    mode = os.environ.get("FAKE_CAPS", "ok")
    if mode == "bad":
        print("not json"); sys.exit(0)
    if mode == "fail":
        sys.exit(5)
    print(json.dumps({{"exit_codes": {{"ok": 0, "task_failure": 1}}, "providers": ["mock"],
                       "schema_version": 1, "spec_version": "1", "harness": "loom"}}))
    sys.exit(0)
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if "--trace" in a:
    with open(a[a.index("--trace") + 1], "w") as f:
        f.write(json.dumps({{"event": "start"}}) + "\n" + "garbage\n" + json.dumps({{"event": "end"}}) + "\n")
rc = int(os.environ.get("FAKE_RC", "0"))
if rc == 3:
    print(json.dumps({{"escalate": "need human"}}))
else:
    info = {{"args": a}}
    if "--task-file" in a:
        info["task"] = json.load(open(a[a.index("--task-file") + 1]))
    print(json.dumps(info))
sys.exit(rc)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    p = tmp_path / "loom"
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LOOM_BIN", str(p))
    return str(p)


def test_not_installed(monkeypatch):
    monkeypatch.delenv("LOOM_BIN", raising=False)
    monkeypatch.setattr(loom.shutil, "which", lambda _n: None)
    assert loom.find_loom() is None
    pr = probe()
    assert not pr.available and pr.capabilities is None and pr.error
    r = LoomAdapter().run_prompt("hi")
    assert r.status == "unavailable" and not r.ok and r.returncode is None
    assert LoomAdapter().run_task({"a": 1}).status == "unavailable"


def test_probe_success(fake):
    pr = probe()
    assert pr.available and pr.path == fake
    assert pr.capabilities["providers"] == ["mock"]


@pytest.mark.parametrize("mode", ["bad", "fail"])
def test_probe_bad(fake, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CAPS", mode)
    pr = probe()
    assert not pr.available and pr.error


def test_probe_missing_binary(tmp_path):
    pr = probe(str(tmp_path / "nope"))
    assert not pr.available and pr.error


@pytest.mark.parametrize("rc,status", [(0, "ok"), (1, "task_failure"), (2, "harness_or_policy_error"),
                                       (3, "escalation_required"), (4, "budget"), (130, "interrupted"),
                                       (77, "unknown")])
def test_exit_codes(fake, monkeypatch, rc, status):
    monkeypatch.setenv("FAKE_RC", str(rc))
    r = LoomAdapter().run_prompt("hi", provider="mock", model="m", max_turns=3)
    assert r.returncode == rc and r.status == status
    assert r.ok == (rc == 0) and r.retryable == (rc == 1)
    if rc != 3:
        assert r.result["args"][:2] == ["-p", "hi"]
        assert "--provider" in r.result["args"] and "--max-turns" in r.result["args"]


def test_escalation_json(fake, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "3")
    r = LoomAdapter().run_prompt("hi")
    assert r.result == {"escalate": "need human"}


def test_trace(fake, tmp_path):
    tp = tmp_path / "t.jsonl"
    r = LoomAdapter().run_prompt("hi", trace_path=str(tp))
    assert r.trace == [{"event": "start"}, {"event": "end"}]
    assert r.trace_path == str(tp)


def test_run_task_dict_and_path(fake, tmp_path):
    r = LoomAdapter(bin=fake).run_task({"goal": "x"})
    assert r.ok and r.result["task"] == {"goal": "x"}
    f = tmp_path / "task.json"
    f.write_text('{"goal": "y"}')
    assert LoomAdapter().run_task(f).result["task"] == {"goal": "y"}


def test_timeout(fake, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "5")
    r = LoomAdapter(timeout=0.5).run_prompt("hi")
    assert r.status == "timeout" and not r.ok and r.returncode is None


def test_install_url_is_env_driven():
    assert loom.LOOM_INSTALL_URL == os.environ.get("LOOM_INSTALL_URL")
