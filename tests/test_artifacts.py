import json

from nexus_lite import artifacts as A


def test_root_is_workspace_artifacts(monkeypatch):
    assert A.artifacts_root("/ws") == A.Path("/ws/artifacts")
    monkeypatch.setenv("NEXUS_ARTIFACTS_DIR", "/r")
    assert A.artifacts_root("/ws") == A.Path("/ws/artifacts")


def test_task_dir_pure_and_ensure(tmp_path):
    assert A.task_dir("t1", tmp_path) == tmp_path / "t1"
    assert not (tmp_path / "t1").exists()
    assert A.ensure_task_dir("t1", tmp_path).is_dir()


def test_env_canonical_wins_old_accepted():
    assert A.task_dir_from_env({}) is None
    assert A.task_dir_from_env({A.TASK_DIR_ENV_DEPRECATED: "/old"}) == A.Path("/old")
    both = {A.TASK_DIR_ENV: "/new", A.TASK_DIR_ENV_DEPRECATED: "/old"}
    assert A.task_dir_from_env(both) == A.Path("/new")
    # plural is the root, never a task dir
    assert A.task_dir_from_env({"NEXUS_ARTIFACTS_DIR": "/root"}) is None


def test_export_env_both(tmp_path):
    env = A.export_env({}, tmp_path)
    assert env[A.TASK_DIR_ENV] == env[A.TASK_DIR_ENV_DEPRECATED] == str(tmp_path)


def test_write_result_and_meta(tmp_path):
    A.write_result(tmp_path, "hi", 0)
    assert (tmp_path / A.OUTPUT).read_text() == (tmp_path / A.RESULT).read_text() == "hi"
    assert (tmp_path / A.RC).read_text() == "0"
    A.write_meta(tmp_path, task_id="t", agent="a", status="completed", rc=0, started=1.0, finished=2.0)
    assert json.loads((tmp_path / A.META).read_text())["status"] == "completed"


def test_delegate_layout(tmp_path, monkeypatch):
    from nexus_lite import core
    for k in ("NEXUS_DELEGATED_ROLE", "NEXUS_DELEGATION_DEPTH", "NEXUS_SENSITIVITY", "NEXUS_ARTIFACTS_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEXUS_LITE_DIR", str(tmp_path))
    r = core.delegate("sh -c 'echo $NEXUS_TASK_DIR $NEXUS_ARTIFACT_DIR'", "p", task_id="tx")
    d = tmp_path / "artifacts" / "tx"
    for n in (A.PROMPT, A.OUTPUT, A.RESULT, A.META, A.RC):
        assert (d / n).exists(), n
    assert (d / A.OUTPUT).read_text().split() == [str(d), str(d)]
    assert r["rc"] == 0
