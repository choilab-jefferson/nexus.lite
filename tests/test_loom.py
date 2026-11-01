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
    mode = os.environ.get("LOOM_FAKE_CAPS", "ok")
    if mode == "bad":
        print("not json"); sys.exit(0)
    if mode == "fail":
        sys.exit(5)
    print(json.dumps({{"exit_codes": {{"ok": 0, "task_failure": 1}}, "providers": ["mock"],
                       "schema_version": 1, "spec_version": "1", "harness": "loom",
                       **({{"version": os.environ["LOOM_FAKE_VER"]}} if "LOOM_FAKE_VER" in os.environ else {{}})}}))
    sys.exit(0)
if os.environ.get("LOOM_FAKE_SLEEP"):
    time.sleep(float(os.environ["LOOM_FAKE_SLEEP"]))
if "--trace" in a:
    with open(a[a.index("--trace") + 1], "w") as f:
        f.write(json.dumps({{"event": "start"}}) + "\n" + "garbage\n" + json.dumps({{"event": "end"}}) + "\n")
rc = int(os.environ.get("LOOM_FAKE_RC", "0"))
if os.environ.get("LOOM_FAKE_CRASH"):
    import signal
    os.kill(os.getpid(), signal.SIGKILL)
if os.environ.get("LOOM_FAKE_BAD"):
    print("this is not json"); sys.exit(rc)
if rc == 3:
    print(json.dumps({{"escalate": "need human"}}))
else:
    info = {{"args": a, "env": sorted(os.environ)}}
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
    monkeypatch.setenv("LOOM_FAKE_CAPS", mode)
    pr = probe()
    assert not pr.available and pr.error


def test_probe_missing_binary(tmp_path):
    pr = probe(str(tmp_path / "nope"))
    assert not pr.available and pr.error


@pytest.mark.parametrize("rc,status", [(0, "ok"), (1, "task_failure"), (2, "harness_or_policy_error"),
                                       (3, "escalation_required"), (4, "budget"), (130, "interrupted"),
                                       (77, "unknown")])
def test_exit_codes(fake, monkeypatch, rc, status):
    monkeypatch.setenv("LOOM_FAKE_RC", str(rc))
    r = LoomAdapter().run_prompt("hi", provider="mock", model="m", max_turns=3)
    assert r.returncode == rc and r.status == status
    assert r.ok == (rc == 0) and r.retryable == (rc == 1)
    if rc != 3:
        assert r.result["args"][:2] == ["-p", "hi"]
        assert "--provider" in r.result["args"] and "--max-turns" in r.result["args"]


def test_escalation_json(fake, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_RC", "3")
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
    monkeypatch.setenv("LOOM_FAKE_SLEEP", "5")
    r = LoomAdapter(timeout=0.5).run_prompt("hi")
    assert r.status == "timeout" and not r.ok and r.returncode is None


def test_env_allowlist(fake, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("LOOM_X", "1")
    env = LoomAdapter().run_prompt("hi").result["env"]
    assert "SECRET_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" in env and "LOOM_X" in env


def test_env_passthrough_extension(fake, monkeypatch):
    monkeypatch.setenv("MY_VAR", "1")
    assert "MY_VAR" not in LoomAdapter().run_prompt("hi").result["env"]
    assert "MY_VAR" in LoomAdapter(env_passthrough=["MY_VAR"]).run_prompt("hi").result["env"]


def test_version_missing(fake):
    pr = probe()
    assert pr.version is None and pr.version_ok


def test_version_ok(fake, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_VER", "v0.1.2")
    pr = probe()
    assert pr.version == "v0.1.2" and pr.version_ok


def test_version_old_warns(fake, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_VER", "0.0.9")
    with pytest.warns(UserWarning):
        pr = probe()
    assert not pr.version_ok


def test_version_unparseable(fake, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_VER", "dev")
    pr = probe()
    assert pr.version == "dev" and pr.version_ok


def test_crash_by_signal_is_explicit_status(fake, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_CRASH", "1")
    monkeypatch.setenv("LOOM_X", "1")  # no-op marker; LOOM_* is forwarded
    r = LoomAdapter().run_prompt("hi")
    assert r.returncode < 0 and r.status == "crash" and not r.ok and not r.retryable


def test_rc0_non_json_is_bad_output_not_ok(fake, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_BAD", "1")
    r = LoomAdapter().run_prompt("hi")
    assert r.returncode == 0 and r.status == "bad_output"
    assert not r.ok and r.result is None and "not json" in r.stdout


def test_nonzero_non_json_keeps_exit_status(fake, monkeypatch):
    monkeypatch.setenv("LOOM_FAKE_BAD", "1")
    monkeypatch.setenv("LOOM_FAKE_RC", "1")
    r = LoomAdapter().run_prompt("hi")
    assert r.status == "task_failure" and r.retryable and not r.ok
