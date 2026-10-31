"""
test_nexus_lite.py — End-to-end tests for nexus-lite.

Covers:
- dtach wire protocol constants and packet serialization
- Real dtach PTY spawn, send, keys, read, wait, list_sessions, kill
- Provider busy/idle classification (claude, agy, gemini, copilot, opencode, bash)
- Bus send & read, inbox markdown persistence, and live PTY pointer line injection
- Delegate execution, artifact creation, depth enforcement, and TASK_COMPLETE notifications
- Wiki read, write, search (FTS5), and list
- CLI subcommands integration
- Protocol pointer formatting, parsing, and truncation
- Role tier rankings and verifier clamping
- PTY key chord encoding and agent command normalization
- Lazy loading of optional submodules (bridge, demos, viz)
"""

import json
import os
import shutil
import subprocess
import sys
import time
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

import nexus_lite as nl
from nexus_lite import bridge, cli, core, protocol, pty, roles


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Isolate workspace and socket paths per test."""
    ws = tmp_path / "nexus_ws"
    socks = tmp_path / "socks"
    ws.mkdir(parents=True, exist_ok=True)
    socks.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("NEXUS_LITE_HOME", str(ws))
    monkeypatch.setenv("NEXUS_LITE_SOCK_DIR", str(socks))
    monkeypatch.setenv("NEXUS_DELEGATION_DEPTH", "0")

    nl.init(ws)
    yield ws

    # Clean up any remaining sessions
    for s in pty.list_sessions():
        pty.kill(s["name"])


def test_pty_wire_constants():
    """Verify dtach 10-byte wire protocol definitions."""
    assert pty.MSG_PUSH == 0
    assert pty.MSG_ATTACH == 1
    assert pty.MSG_DETACH == 2
    assert pty.MSG_WINCH == 3
    assert pty.MSG_REDRAW == 4
    assert pty._PKT.size == 10


def test_pty_classifier():
    """Verify is_busy and is_idle across known agent providers."""
    # Claude
    assert pty.is_busy("✻ Cogitating… (4s · ↓ 305 tokens)", provider="claude")
    assert pty.is_busy("esc to interrupt", provider="claude")
    assert pty.is_idle("❯ Write your code", provider="claude")

    # Agy
    assert pty.is_busy("Analyzing…", provider="agy")
    assert pty.is_busy("esc to cancel", provider="agy")
    assert pty.is_idle("accept-edits", provider="agy")

    # Gemini
    assert pty.is_busy("Thinking…", provider="gemini")
    assert pty.is_busy("esc to cancel", provider="gemini")
    assert pty.is_idle("Type your message", provider="gemini")

    # Copilot
    assert pty.is_busy("Working", provider="copilot")
    assert pty.is_idle("Do you want to run this command?", provider="copilot")

    # OpenCode
    assert pty.is_busy("Thinking…", provider="opencode")
    assert pty.is_idle("Permission required", provider="opencode")

    # Bash
    assert pty.is_idle("user@host:~$ ", provider="bash")
    assert pty.is_idle("bash-5.2$ ", provider="bash")
    assert not pty.is_busy("user@host:~$ ", provider="bash")


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_real_dtach_pty_lifecycle():
    """Test real dtach PTY spawn, send, read, keys, and kill."""
    sess_name = "test-pty-1"
    name = pty.spawn("bash --norc", name=sess_name)
    assert name == sess_name

    sessions = pty.list_sessions()
    assert any(s["name"] == sess_name for s in sessions)

    pty.send(sess_name, "echo PTY_ECHO_VAL_999", enter=True)
    screen = pty.wait(sess_name, provider="bash", timeout=5.0)
    assert "PTY_ECHO_VAL_999" in screen

    # Test keys
    pty.keys(sess_name, "c-c")
    time.sleep(0.1)

    assert pty.kill(sess_name)
    sessions_after = pty.list_sessions()
    assert not any(s["name"] == sess_name for s in sessions_after)


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_bus_send_pointer_injection(isolated_env):
    """Test bus_send writes markdown inbox and injects pointer line into live dtach session."""
    receiver_name = "receiver-sess"
    pty.spawn("bash --norc", name=receiver_name)

    msg_text = "Task notification: please review changes\nLine two content"
    res = nl.bus_send(
        target=receiver_name,
        message=msg_text,
        sender="worker-1",
        kind="dm",
    )

    assert res["target"] == receiver_name
    assert res["summary"] == "Task notification: please review changes"
    msg_id = res["id"]
    inbox_file = Path(res["path"])
    assert inbox_file.exists()
    assert "Line two content" in inbox_file.read_text(encoding="utf-8")

    # Check SQLite record
    msgs = nl.bus_read(target=receiver_name)
    assert any(m["id"] == msg_id for m in msgs)

    # Check PTY received the injected pointer line
    time.sleep(0.3)
    screen = pty.read(receiver_name, settle=0.1)
    expected_pointer_prefix = f"[nexus] dm {msg_id}"
    assert expected_pointer_prefix in screen

    pty.kill(receiver_name)


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_delegate_execution_and_notification(isolated_env):
    """Test task delegation creates artifacts, updates DB, and notifies caller PTY."""
    caller_sess = "caller-agent"
    pty.spawn("bash --norc", name=caller_sess)

    worker_script = "import sys; p = sys.stdin.read().strip(); print(f'PROCESSED: {p}')"
    res = nl.delegate(
        agent_cmd=f"{sys.executable} -c {repr(worker_script)}",
        prompt="hello_worker_agent",
        caller=caller_sess,
        bg=False,
    )

    assert res["status"] == "completed"
    assert res["rc"] == 0
    task_id = res["task_id"]
    art_dir = Path(res["artifact_dir"])

    assert (art_dir / "prompt.md").read_text() == "hello_worker_agent"
    assert "PROCESSED: hello_worker_agent" in (art_dir / "output.md").read_text()
    assert (art_dir / "rc").read_text().strip() == "0"

    # Check DB record
    with nl.get_db() as con:
        row = con.execute("SELECT status, rc FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None
        assert row["status"] == "completed"
        assert row["rc"] == 0

    # Check that caller received TASK_COMPLETE in PTY
    time.sleep(0.3)
    screen = pty.read(caller_sess, settle=0.1)
    assert f"TASK_COMPLETE {task_id}" in screen

    pty.kill(caller_sess)


def test_delegation_depth_limit(monkeypatch):
    """Verify recursion guard raises PermissionError when depth >= 2."""
    monkeypatch.setenv("NEXUS_DELEGATION_DEPTH", "2")
    with pytest.raises(PermissionError) as exc_info:
        nl.delegate("echo test", "prompt")
    assert "NEXUS_DELEGATION_DEPTH exceeded" in str(exc_info.value)


def test_wiki_operations(isolated_env):
    """Test wiki write, read, list, and FTS5 full-text search."""
    slug1 = "protocols/communication"
    content1 = "# Communication Protocol\nNexus uses Redis Streams for MTM and SQLite for lite bus."
    p1 = nl.wiki_write(slug1, content1)
    assert p1.exists()

    slug2 = "architecture/storage"
    content2 = "# Storage Architecture\nLite stores artifacts under .nexus/artifacts and wiki markdown."
    nl.wiki_write(slug2, content2)

    # Read
    read_back = nl.wiki_read(slug1)
    assert read_back == content1

    # List
    pages = nl.wiki_list()
    slugs = [p["slug"] for p in pages]
    assert slug1 in slugs
    assert slug2 in slugs

    # FTS5 Search
    hits_redis = nl.wiki_search("Redis")
    assert any(h["slug"] == slug1 for h in hits_redis)

    hits_storage = nl.wiki_search("Storage")
    assert any(h["slug"] == slug2 for h in hits_storage)

    hits_none = nl.wiki_search("NonExistentTermXYZ")
    assert len(hits_none) == 0


def test_cli_integration(isolated_env, capsys):
    """Test CLI commands execution."""
    # CLI init
    ret = cli.main(["init"])
    assert ret == 0

    # CLI wiki write & read & search
    ret = cli.main(["wiki", "write", "cli-test", "# CLI Markdown Content"])
    assert ret == 0

    ret = cli.main(["wiki", "read", "cli-test"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "# CLI Markdown Content" in captured.out

    ret = cli.main(["wiki", "search", "Markdown"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "cli-test" in captured.out

    # CLI bus send & read
    ret = cli.main(["bus", "send", "user1", "Hello via CLI bus"])
    assert ret == 0

    ret = cli.main(["bus", "read"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "Hello via CLI bus" in captured.out


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_chat_helper(isolated_env):
    """Test chat helper sends message and waits for rendered output."""
    sess_name = "chat-sess"
    pty.spawn("bash --norc", name=sess_name)

    output = nl.chat(sess_name, "echo 'CHAT_OUTPUT_CONFIRMED'", provider="bash", timeout=5.0)
    assert "CHAT_OUTPUT_CONFIRMED" in output
    pty.kill(sess_name)


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_delegate_background(isolated_env):
    """Test delegate in background mode (bg=True)."""
    caller_sess = "bg-caller"
    pty.spawn("bash --norc", name=caller_sess)

    res = nl.delegate(
        agent_cmd="python3 -c 'import time; time.sleep(0.3); print(\"BG_WORKER_DONE\")'",
        prompt="run in bg",
        caller=caller_sess,
        bg=True,
    )

    assert res["status"] == "running"
    task_id = res["task_id"]

    # Poll until completed
    deadline = time.monotonic() + 5.0
    completed = False
    while time.monotonic() < deadline:
        with nl.get_db() as con:
            row = con.execute("SELECT status, rc FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row and row["status"] == "completed":
                completed = True
                break
        time.sleep(0.1)

    assert completed
    art_dir = Path(res["artifact_dir"])
    assert "BG_WORKER_DONE" in (art_dir / "output.md").read_text()
    pty.kill(caller_sess)


def test_ensure_colab(tmp_path, monkeypatch):
    """Test ensure_colab initializes workspace and honors custom drive path and gemini sync."""
    drive_dir = tmp_path / "google_drive"
    gemini_drive = tmp_path / "gemini_drive"
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    ws = nl.ensure_colab(drive_path=str(drive_dir), gemini_drive_path=str(gemini_drive))
    assert ws == drive_dir.resolve()
    assert (ws / "lite.db").exists()
    assert (ws / "wiki").exists()
    assert (fake_home / ".gemini").is_symlink()
    assert (fake_home / ".gemini").resolve() == gemini_drive.resolve()


def test_safe_gemini_drive_sync_no_overwrite(tmp_path):
    """Test sync_dotdir_to_drive preserves Drive credentials, copies local-only files, and symlinks."""
    local_dir = tmp_path / "local_gemini"
    drive_dir = tmp_path / "drive_gemini"

    local_dir.mkdir(parents=True)
    (local_dir / "auth.json").write_text("NEW_EMPTY", encoding="utf-8")
    (local_dir / "local_plugin.txt").write_text("keep_me", encoding="utf-8")

    drive_dir.mkdir(parents=True)
    (drive_dir / "auth.json").write_text("SAVED_TOKEN", encoding="utf-8")

    ret = nl.sync_dotdir_to_drive(local_dir, drive_dir)
    assert ret == drive_dir.resolve()

    # Preserves SAVED_TOKEN in drive_dir/auth.json (Drive wins)
    assert (drive_dir / "auth.json").read_text(encoding="utf-8") == "SAVED_TOKEN"

    # Copies local_plugin.txt into drive_dir
    assert (drive_dir / "local_plugin.txt").read_text(encoding="utf-8") == "keep_me"

    # Turns local_dir into a symlink to drive_dir
    assert local_dir.is_symlink()
    assert local_dir.resolve() == drive_dir.resolve()

    # Reading through local_dir accesses the preserved drive_dir contents
    assert (local_dir / "auth.json").read_text(encoding="utf-8") == "SAVED_TOKEN"
    assert (local_dir / "local_plugin.txt").read_text(encoding="utf-8") == "keep_me"

    # Idempotent re-run on existing symlink returns drive_dir
    ret2 = nl.sync_dotdir_to_drive(local_dir, drive_dir)
    assert ret2 == drive_dir.resolve()
    assert local_dir.is_symlink()


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_cli_pty_and_delegate(isolated_env, capsys):
    """Test CLI commands for pty and delegate."""
    # CLI pty new
    ret = cli.main(["pty", "new", "bash --norc", "--name", "cli-pty-sess"])
    assert ret == 0

    # CLI pty list
    ret = cli.main(["pty", "list"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "cli-pty-sess" in captured.out

    # CLI pty send & wait
    ret = cli.main(["pty", "send", "cli-pty-sess", "echo CLI_PTY_OK"])
    assert ret == 0

    ret = cli.main(["pty", "wait", "cli-pty-sess", "--provider", "bash", "--timeout", "5.0"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "CLI_PTY_OK" in captured.out

    # CLI delegate
    ret = cli.main([
        "delegate",
        "python3 -c 'print(\"CLI_DELEGATE_SUCCESS\")'",
        "instruction payload",
    ])
    assert ret == 0
    captured = capsys.readouterr()
    assert "CLI_DELEGATE_SUCCESS" in captured.out
    assert "completed" in captured.out

    # CLI pty kill
    ret = cli.main(["pty", "kill", "cli-pty-sess"])
    assert ret == 0


def test_agy_normalization():
    """Verify command normalization for agy in pty and oneshot modes."""
    # PTY mode
    assert pty.normalize_agent_cmd("agy", mode="pty") == "agy --dangerously-skip-permissions"
    assert (
        pty.normalize_agent_cmd("agy --dangerously-skip-permissions", mode="pty")
        == "agy --dangerously-skip-permissions"
    )
    assert pty.normalize_agent_cmd(["agy"], mode="pty") == "agy --dangerously-skip-permissions"

    # Oneshot mode
    oneshot_cmd = pty.normalize_agent_cmd("agy", mode="oneshot", prompt="hello world")
    assert "agy --dangerously-skip-permissions -p" in oneshot_cmd
    assert "hello world" in oneshot_cmd

    # Non-agy passthrough
    assert pty.normalize_agent_cmd("bash", mode="pty") == "bash"
    assert pty.normalize_agent_cmd(["ls", "-la"], mode="pty") == ["ls", "-la"]


@pytest.mark.skipif(shutil.which("agy") is None, reason="agy binary not available")
def test_live_agy_delegate_oneshot():
    """Verify live agy execution via delegate in oneshot mode."""
    res = nl.delegate(
        agent_cmd="agy",
        prompt="Reply with exactly: HELLO_AGY_CONFIRMED",
        caller="main",
        bg=False,
        timeout=60.0,
    )
    out = res.get("output", "")
    if res["rc"] != 0 and any(k in out.lower() for k in ("login", "auth", "credentials", "timeout", "sign in")):
        pytest.skip(f"agy installed but not authenticated: {out[:120]}")
    assert res["status"] == "completed"
    assert res["rc"] == 0
    assert "HELLO_AGY_CONFIRMED" in out
    art_dir = Path(res["artifact_dir"])
    assert (art_dir / "output.md").exists()
    assert "HELLO_AGY_CONFIRMED" in (art_dir / "output.md").read_text()


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_multi_agent_swarm_and_pipeline(isolated_env):
    """
    Test real multi-agent orchestration:
    1. Spawn orchestrator PTY session.
    2. Run 3-worker parallel swarm (run_swarm) generating code/data artifacts and notifying orchestrator PTY.
    3. Run 3-stage pipeline (run_pipeline: driver -> verifier -> documenter).
    4. Assert artifacts, bus HANDOFF/TASK_COMPLETE messages, and orchestrator PTY pointer lines.
    """
    ws = isolated_env
    # 1. Spawn orchestrator PTY session
    orch = pty.spawn("bash --norc", name="orchestrator")
    assert orch == "orchestrator"
    pty.wait("orchestrator", provider="bash", timeout=5.0)

    # 2. Run 3-worker parallel swarm
    specs = [
        {
            "task_id": "swarm-data-gen",
            "cmd": "python3 -c 'import os, json; d=os.environ.get(\"NEXUS_ARTIFACT_DIR\", \".\"); open(os.path.join(d, \"data.json\"), \"w\").write(json.dumps({\"status\": \"ok\", \"worker\": 1})); print(\"SWARM_DATA_GENERATED\")'",
            "prompt": "Worker 1: generate dataset",
        },
        {
            "task_id": "swarm-calc-val",
            "cmd": "python3 -c 'import os; d=os.environ.get(\"NEXUS_ARTIFACT_DIR\", \".\"); open(os.path.join(d, \"result.txt\"), \"w\").write(\"42\"); print(\"SWARM_CALC_DONE\")'",
            "prompt": "Worker 2: calculate value",
        },
        {
            "task_id": "swarm-report-gen",
            "cmd": "python3 -c 'import os; d=os.environ.get(\"NEXUS_ARTIFACT_DIR\", \".\"); open(os.path.join(d, \"report.md\"), \"w\").write(\"# Swarm Report\\nMetrics ready.\"); print(\"SWARM_REPORT_READY\")'",
            "prompt": "Worker 3: generate summary",
        },
    ]

    swarm_results = nl.run_swarm(specs, caller="orchestrator", timeout=30.0)
    assert len(swarm_results) == 3
    for res in swarm_results:
        assert res["status"] == "completed"
        assert res["rc"] == 0
        art_p = Path(res["artifact_dir"])
        assert (art_p / "output.md").exists()
        assert (art_p / "rc").read_text().strip() == "0"

    assert (Path(swarm_results[0]["artifact_dir"]) / "data.json").exists()
    assert (Path(swarm_results[1]["artifact_dir"]) / "result.txt").exists()
    assert (Path(swarm_results[2]["artifact_dir"]) / "report.md").exists()

    # 3. Run 3-stage pipeline (driver -> verifier -> documenter)
    stages = [
        {
            "name": "driver",
            "cmd": "python3 -c 'open(\"geometry_mod.py\", \"w\").write(\"def compute_area(w, h): return w * h\\n\"); print(\"DRIVER_WROTE_MODULE\")'",
            "prompt": "Stage 1: write geometry module",
        },
        {
            "name": "verifier",
            "cmd": "python3 -c 'import geometry_mod; assert geometry_mod.compute_area(6, 7) == 42; print(\"VERIFIER_CONFIRMED_SUCCESS\")'",
            "prompt": "Stage 2: verify geometry module: {prev_output}",
        },
        {
            "name": "documenter",
            "cmd": "python3 -c 'import nexus_lite as nl; nl.wiki_write(\"modules/geometry\", \"# Geometry Module\\n\\nVerified compute_area returns 42.\"); print(\"DOCUMENTER_WROTE_WIKI\")'",
            "prompt": "Stage 3: document geometry module: {prev_output}",
        },
    ]

    pipe_res = nl.run_pipeline(
        stages,
        initial_prompt="Initiate geometry module construction",
        caller="orchestrator",
        timeout=30.0,
    )
    assert len(pipe_res["stages"]) == 3
    for s_res in pipe_res["stages"]:
        assert s_res["status"] == "completed"
        assert s_res["rc"] == 0

    assert (ws / "geometry_mod.py").exists()
    assert "VERIFIER_CONFIRMED_SUCCESS" in pipe_res["stages"][1]["output"]
    assert "DOCUMENTER_WROTE_WIKI" in pipe_res["final_output"]

    # Assert wiki document created
    wiki_text = nl.wiki_read("modules/geometry")
    assert "Geometry Module" in wiki_text
    assert "compute_area" in wiki_text

    # Assert bus messages: HANDOFF and TASK_COMPLETE
    bus_msgs = nl.bus_read(limit=50)
    handoff_msgs = [m for m in bus_msgs if m["kind"] == "handoff"]
    task_msgs = [m for m in bus_msgs if m["kind"] == "task"]
    assert len(handoff_msgs) >= 2  # driver->verifier, verifier->documenter
    assert len(task_msgs) >= 6     # 3 swarm workers + 3 pipeline stages

    # Assert orchestrator PTY pointer lines
    time.sleep(0.3)
    orch_screen = pty.read("orchestrator", cols=160, rows=50, settle=0.2)
    assert "[nexus]" in orch_screen
    assert "TASK_COMPLETE" in orch_screen
    assert "handoff" in orch_screen or "HANDOFF" in orch_screen

    # Cleanup
    pty.kill("orchestrator")


@pytest.mark.skipif(shutil.which("dtach") is None, reason="dtach binary not available")
def test_visualization_dashboard(isolated_env, capsys):
    """
    Test live HTML5/Colab visualization dashboard:
    - Runs after multi-agent activity.
    - Calls nl.dashboard(display_inline=False) and cli.main(["dashboard"]).
    - Asserts HTML contains SVG topology (<svg), live PTY screen text, task outputs, and bus messages.
    """
    ws = isolated_env

    # 1. Spawn an active session with specific screen content
    sess_name = "viz-active-agent"
    pty.spawn("bash --norc", name=sess_name)
    pty.send(sess_name, "echo 'VIZ_TERMINAL_SCREEN_MARKER_789'", enter=True)
    pty.wait(sess_name, provider="bash", timeout=5.0)

    # 2. Run a task and send a bus message
    task_res = nl.delegate(
        "python3 -c 'print(\"VIZ_TASK_OUTPUT_SAMPLE_XYZ\")'",
        prompt="Sample task for visualization",
        caller=sess_name,
    )
    assert task_res["status"] == "completed"

    nl.bus_send(
        target=sess_name,
        message="VIZ_BUS_MESSAGE_SAMPLE_ABC",
        sender="viz-orchestrator",
        kind="dm",
    )

    # 3. Call nl.dashboard(display_inline=False)
    html_content = nl.dashboard(display_inline=False)
    assert isinstance(html_content, str)
    assert "<!DOCTYPE html>" in html_content
    # Assert SVG topology
    assert "<svg" in html_content
    assert "</svg>" in html_content
    assert "ORCHESTRATORS" in html_content
    assert "WORKERS" in html_content
    # Assert live PTY screen text
    assert "VIZ_TERMINAL_SCREEN_MARKER_789" in html_content
    # Assert task output and ID
    assert "VIZ_TASK_OUTPUT_SAMPLE_XYZ" in html_content
    assert task_res["task_id"] in html_content
    # Assert bus message
    assert "VIZ_BUS_MESSAGE_SAMPLE_ABC" in html_content

    # 4. Call CLI subcommand: nexus-lite dashboard
    cli_out_path = ws / "custom_dashboard.html"
    ret = cli.main(["dashboard", "--out", str(cli_out_path)])
    assert ret == 0
    assert cli_out_path.exists()
    cli_html = cli_out_path.read_text(encoding="utf-8")
    assert "<svg" in cli_html
    assert "VIZ_TERMINAL_SCREEN_MARKER_789" in cli_html

    # Also test default CLI dashboard path (.nexus/dashboard.html)
    ret_default = cli.main(["dashboard"])
    assert ret_default == 0
    default_p = ws / "dashboard.html"
    assert default_p.exists()

    pty.kill(sess_name)


@pytest.mark.skipif(shutil.which("agy") is None, reason="agy binary not available")
def test_live_agy_multi_agent_pipeline():
    """
    Test 2-stage live agy pipeline (driver -> verifier).
    Skips cleanly if unauthenticated.
    """
    stages = [
        {
            "name": "driver",
            "cmd": "agy",
            "prompt": "Reply with exactly: AGY_DRIVER_STAGE_SUCCESS",
        },
        {
            "name": "verifier",
            "cmd": "agy",
            "prompt": "The previous stage produced: '{prev_output}'. Reply with exactly: AGY_VERIFIER_CONFIRMED_SUCCESS",
        },
    ]

    try:
        res = nl.run_pipeline(
            stages,
            initial_prompt="Start live agy pipeline",
            caller="main",
            timeout=90.0,
        )
    except Exception as e:
        pytest.skip(f"agy execution error: {e}")

    driver_out = res["stages"][0].get("output", "")
    if res["stages"][0]["rc"] != 0 and any(
        k in driver_out.lower()
        for k in ("login", "auth", "credentials", "timeout", "sign in")
    ):
        pytest.skip(f"agy not authenticated: {driver_out[:120]}")

    assert len(res["stages"]) == 2
    assert res["stages"][0]["status"] == "completed"
    assert res["stages"][0]["rc"] == 0
    assert "AGY_DRIVER_STAGE_SUCCESS" in res["stages"][0]["output"]

    assert res["stages"][1]["status"] == "completed"
    assert res["stages"][1]["rc"] == 0
    assert "AGY_VERIFIER_CONFIRMED_SUCCESS" in res["stages"][1]["output"]
    assert "AGY_VERIFIER_CONFIRMED_SUCCESS" in res["final_output"]


def test_bridge_server_and_remote_colab_client(isolated_env):
    """
    Test cloudflared remote bridge server and RemoteColab client.
    Verifies authentication, remote exec, PTY, delegation, swarm, bus, wiki, and dashboard.
    """
    import urllib.error

    # 1. Start bridge on ephemeral port without trycloudflare tunnel
    info = nl.start_bridge(port=0, tunnel=False)
    assert info["port"] > 0
    assert info["local_url"].startswith(f"http://127.0.0.1:{info['port']}")
    assert info["token"]
    assert "dashboard_url" in info

    try:
        # 2. Verify unauthorized requests return 401/403
        bad_remote = nl.RemoteColab(info["local_url"], token="invalid-token-xyz")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            bad_remote.health()
        assert exc_info.value.code in (401, 403)

        no_token_remote = nl.RemoteColab(info["local_url"], token="")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            no_token_remote.health()
        assert exc_info.value.code in (401, 403)

        # 3. Authorized client
        remote = nl.RemoteColab(info["local_url"], info["token"])

        # 4. remote.health()
        h = remote.health()
        assert h["status"] == "ok"
        assert "workspace" in h

        # 5. remote.exec(python="print(6*7)") and remote.exec(cmd=...)
        py_res = remote.exec(python="print(6*7)")
        assert py_res["rc"] == 0
        assert "42" in py_res["stdout"].strip()

        # Verify stdin=subprocess.DEVNULL: stdin reads encounter immediate EOF rather than hanging
        py_devnull = remote.exec(python="import sys; print('STDIN_EOF:', len(sys.stdin.read()))")
        assert py_devnull["rc"] == 0
        assert "STDIN_EOF: 0" in py_devnull["stdout"]

        cmd_res = remote.exec(cmd="echo 'BRIDGE_EXEC_OK'")
        assert cmd_res["rc"] == 0
        assert "BRIDGE_EXEC_OK" in cmd_res["stdout"]

        cmd_devnull = remote.exec(cmd="head -n 1")
        assert cmd_devnull["rc"] == 0

        # 6. remote.pty_spawn / pty_send / pty_read / pty_kill
        if shutil.which("dtach"):
            sess = remote.pty_spawn("bash --norc", name="rem-pty-test")
            assert sess == "rem-pty-test"
            remote.pty_send(sess, "echo 'REMOTE_PTY_HELLO'", enter=True)
            screen = remote.pty_wait(sess, provider="bash", timeout=5.0)
            assert "REMOTE_PTY_HELLO" in screen
            remote.pty_kill(sess)

        # 7. remote.delegate
        del_res = remote.delegate(
            agent_cmd="python3 -c 'print(\"REMOTE_DELEGATE_SUCCESS\")'",
            prompt="Compute result",
            caller="main",
            timeout=30.0,
        )
        assert del_res["status"] == "completed"
        assert del_res["rc"] == 0
        assert "REMOTE_DELEGATE_SUCCESS" in del_res["output"]

        # 8. remote.run_swarm
        swarm_specs = [
            {"task_id": "sw-rem-1", "cmd": "python3 -c 'print(\"SWARM_1\")'", "prompt": "p1"},
            {"task_id": "sw-rem-2", "cmd": "python3 -c 'print(\"SWARM_2\")'", "prompt": "p2"},
        ]
        swarm_res = remote.run_swarm(swarm_specs, caller="orchestrator", timeout=30.0)
        assert len(swarm_res) == 2
        assert all(r["status"] == "completed" for r in swarm_res)
        assert "SWARM_1" in swarm_res[0]["output"]
        assert "SWARM_2" in swarm_res[1]["output"]

        # 9. remote.bus_send / bus_read
        msg = remote.bus_send(target="test-agent", message="Ping from RemoteColab", sender="client", kind="dm")
        assert msg["id"]
        assert msg["target"] == "test-agent"
        msgs = remote.bus_read(target="test-agent", limit=10)
        assert any(m["id"] == msg["id"] for m in msgs)

        # 10. remote.wiki_write / wiki_search / wiki_read
        path = remote.wiki_write("docs/remote_bridge", "# Remote Bridge\nZero-tunnel testing.")
        assert path
        read_content = remote.wiki_read("docs/remote_bridge")
        assert "Zero-tunnel testing" in read_content
        hits = remote.wiki_search("Zero-tunnel")
        assert len(hits) >= 1
        assert hits[0]["slug"] == "docs/remote_bridge"

        # 11. remote.dashboard_html()
        html = remote.dashboard_html()
        assert "<!DOCTYPE html>" in html or "html" in html.lower()

    finally:
        # 12. stop_bridge()
        nl.stop_bridge()


def test_cli_bridge(isolated_env, capsys):
    """Test CLI commands for bridge status, exec, and stop."""
    info = nl.start_bridge(port=0, tunnel=False)
    try:
        # bridge status
        rc = cli.main(["bridge", "status"])
        assert rc == 0
        captured = capsys.readouterr()
        assert str(info["port"]) in captured.out

        # bridge exec
        rc = cli.main(["bridge", "exec", "echo 'CLI_EXEC_HELLO'", "--url", info["local_url"], "--token", info["token"]])
        assert rc == 0
        captured = capsys.readouterr()
        assert "CLI_EXEC_HELLO" in captured.out
    finally:
        nl.stop_bridge()


def test_ensure_cloudflared_unit(monkeypatch, tmp_path):
    """Verify ensure_cloudflared discovers binary from PATH."""
    fake_bin = tmp_path / "cloudflared"
    fake_bin.write_text("#!/bin/sh\necho fake\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    p = bridge.ensure_cloudflared()
    assert Path(p).is_file()


def test_dashboard_2x2_layout_and_bridge_buttons(isolated_env):
    """Test compact 2x2 responsive widescreen grid and bridge.json external action buttons."""
    ws = isolated_env

    # 1. Without bridge.json
    html_nobridge = nl.render_dashboard_html()
    assert "minmax(420px, 1fr)" in html_nobridge
    assert "max-height: 280px" in html_nobridge
    assert "Open Live Dashboard (Cloudflare Tunnel) ↗" not in html_nobridge

    # 2. Write bridge.json with dashboard_url and colab_dashboard_url
    binfo = {
        "dashboard_url": "https://test-subdomain.trycloudflare.com/dashboard?token=xyz123",
        "colab_dashboard_url": "https://colab-proxy.googleusercontent.com/dashboard?token=xyz123",
    }
    (ws / "bridge.json").write_text(json.dumps(binfo), encoding="utf-8")

    html_with_bridge = nl.render_dashboard_html()
    assert "Open Live Dashboard (Cloudflare Tunnel) ↗" in html_with_bridge
    assert 'href="https://test-subdomain.trycloudflare.com/dashboard?token=xyz123&refresh=3"' in html_with_bridge
    assert 'target="_blank"' in html_with_bridge
    assert "Open Live Dashboard (Colab Proxy) ↗" in html_with_bridge
    assert 'href="https://colab-proxy.googleusercontent.com/dashboard?token=xyz123"' in html_with_bridge


def test_dashboard_ipython_display_return_none(monkeypatch):
    """Test dashboard(return_html=False) displays HTML and returns None when IPython is present."""
    import types

    displayed = []

    class _FakeHTML:
        def __init__(self, data: str):
            self.data = data

    ipd_mod = types.ModuleType("IPython.display")
    ipd_mod.HTML = _FakeHTML
    ipd_mod.display = lambda obj: displayed.append(obj)

    ipython_mod = types.ModuleType("IPython")
    ipython_mod.display = ipd_mod

    monkeypatch.setitem(sys.modules, "IPython", ipython_mod)
    monkeypatch.setitem(sys.modules, "IPython.display", ipd_mod)

    # Default return_html=False -> should display and return None
    res = nl.dashboard(display_inline=True, return_html=False)
    assert res is None
    assert len(displayed) >= 1
    assert "<!DOCTYPE html>" in displayed[-1].data

    # Explicit return_html=True -> returns html
    res_html = nl.dashboard(display_inline=True, return_html=True)
    assert isinstance(res_html, str)
    assert "<!DOCTYPE html>" in res_html


def test_colab_poll_callback_and_button(monkeypatch):
    """Test dashboard registers google.colab callback 'nexus_lite.poll_dashboard' and renders poll button."""
    import types

    google_mod = types.ModuleType("google")
    colab_mod = types.ModuleType("google.colab")
    output_mod = types.ModuleType("google.colab.output")
    callbacks = {}
    output_mod.register_callback = lambda name, cb: callbacks.__setitem__(name, cb)
    output_mod.JSON = lambda x: x
    colab_mod.output = output_mod
    google_mod.colab = colab_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.colab", colab_mod)
    monkeypatch.setitem(sys.modules, "google.colab.output", output_mod)

    html = nl.render_dashboard_html()
    assert "↻ Auto-Poll (Colab Fullscreen)" in html
    assert "nexus_lite.poll_dashboard" in html

    nl.dashboard(display_inline=False)
    assert "nexus_lite.poll_dashboard" in callbacks
    data = callbacks["nexus_lite.poll_dashboard"]()
    assert "html" in data
    assert "Nexus-Lite Multi-Agent Dashboard" in data["html"]


def test_bridge_refresh_meta_tag(isolated_env):
    """Test /dashboard?refresh=3 and /?refresh=3 inject auto-refresh meta tag."""
    import urllib.request

    info = nl.start_bridge(port=0, tunnel=False)
    try:
        hdr = {"Authorization": f"Bearer {info['token']}"}
        url_with_refresh = f"{info['local_url']}/dashboard?refresh=3"
        req = urllib.request.Request(url_with_refresh, headers=hdr)
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            content = resp.read().decode("utf-8")
        assert "<meta http-equiv='refresh' content='3'>" in content

        # Also test root path /?token=...&refresh=5
        url_root = f"{info['local_url']}/?refresh=5"
        req_root = urllib.request.Request(url_root, headers=hdr)
        with urllib.request.urlopen(req_root, timeout=5.0) as resp:
            root_content = resp.read().decode("utf-8")
        assert "<meta http-equiv='refresh' content='5'>" in root_content
    finally:
        nl.stop_bridge()


def test_roles_exports_and_dispatch():
    """Test Open-Core Kernel Phase 1 roles exports and role dispatch resolution."""
    assert "driver" in nl.ROLE_PROMPTS
    assert "navigator" in nl.ROLE_PROMPTS
    assert "refiner" in nl.ROLE_PROMPTS
    assert "verifier" in nl.ROLE_PROMPTS
    assert "thinker" in nl.ROLE_PROMPTS
    assert "worker" in nl.ROLE_PROMPTS

    assert nl.ROLE_FOR_AGENT["mini-driver"] == "driver"
    assert nl.ROLE_FOR_AGENT["mini-nexus"] == "thinker"

    assert "mini-worker" in nl.REAL_AGENTS
    assert "mini-nexus" in nl.REAL_AGENTS

    agent, prompt, role = nl.resolve_role_dispatch("mini-driver", "Write code")
    assert agent == "mini-worker"
    assert role == "driver"
    assert "[ROLE: driver]" in prompt
    assert "Write code" in prompt


def test_colab_demo_notebook_compiles():
    """Test that all code cells in notebooks/nexus_lite_colab_demo.ipynb compile without SyntaxError."""
    nb_path = REPO_ROOT / "notebooks" / "nexus_lite_colab_demo.ipynb"
    assert nb_path.exists()
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    compiled_count = 0
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] == "code":
            src = "".join(c["source"])
            if not src.lstrip().startswith("!"):
                compile(src, f"cell_{i}", "exec")
                compiled_count += 1
    assert compiled_count >= 4


def test_detect_env_variants(monkeypatch):
    """Test detect_env detection across Colab, Codespaces, Devcontainer, Docker, and Local."""
    # 1. Default on current system
    current = nl.detect_env()
    assert current in ("colab", "codespaces", "devcontainer", "docker", "local")

    # 2. Google Colab via sys.modules
    monkeypatch.setitem(sys.modules, "google.colab", object())
    assert nl.detect_env() == "colab"
    monkeypatch.delitem(sys.modules, "google.colab")

    # Google Colab via environment variable
    monkeypatch.setenv("COLAB_RELEASE_TAG", "release-20241028")
    assert nl.detect_env() == "colab"
    monkeypatch.delenv("COLAB_RELEASE_TAG")

    # 3. GitHub Codespaces
    monkeypatch.setenv("CODESPACES", "true")
    assert nl.detect_env() == "codespaces"
    monkeypatch.delenv("CODESPACES")

    # 4. Dev Container via REMOTE_CONTAINERS
    monkeypatch.setenv("REMOTE_CONTAINERS", "true")
    assert nl.detect_env() == "devcontainer"
    monkeypatch.delenv("REMOTE_CONTAINERS")

    # Dev Container via /.dockerenv and /workspaces
    original_exists = Path.exists

    def mock_exists_devcontainer(self):
        if str(self) in ("/.dockerenv", "/workspaces"):
            return True
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", mock_exists_devcontainer)
    assert nl.detect_env() == "devcontainer"

    # 5. Generic Docker container
    def mock_exists_docker(self):
        if str(self) == "/.dockerenv":
            return True
        if str(self) == "/workspaces":
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", mock_exists_docker)
    assert nl.detect_env() == "docker"

    # 6. Local fallback
    def mock_exists_local(self):
        if str(self) in ("/.dockerenv", "/workspaces"):
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", mock_exists_local)
    monkeypatch.delenv("CODESPACES", raising=False)
    monkeypatch.delenv("REMOTE_CONTAINERS", raising=False)
    monkeypatch.delenv("COLAB_RELEASE_TAG", raising=False)
    sys.modules.pop("google.colab", None)
    assert nl.detect_env() == "local"


def test_ensure_env_functionality(tmp_path, monkeypatch):
    """Test ensure_env across non-colab and colab simulated environments."""
    ws = tmp_path / "env_ws"

    # Non-colab test with start_bridge_server=False
    res = nl.ensure_env(workspace=ws, start_bridge_server=False)
    assert res["env"] in ("colab", "codespaces", "devcontainer", "docker", "local")
    assert res["workspace"] == ws
    assert res["bridge"] is None
    assert (ws / "inbox").exists()
    assert (ws / "artifacts").exists()
    assert (ws / "wiki").exists()
    assert (ws / "lite.db").exists()

    # Colab delegation test
    colab_ws = tmp_path / "colab_ws"
    monkeypatch.setitem(sys.modules, "google.colab", object())
    try:
        colab_res = nl.ensure_env(workspace=colab_ws, mount_drive=False, start_bridge_server=False)
        assert colab_res["env"] == "colab"
        assert colab_res["workspace"] == colab_ws
        assert colab_res["bridge"] is None
        assert (colab_ws / "lite.db").exists()
    finally:
        sys.modules.pop("google.colab", None)


def test_devcontainer_json_validity():
    """Test validity and required settings of .devcontainer/devcontainer.json."""
    cfg_path = REPO_ROOT / ".devcontainer" / "devcontainer.json"
    assert cfg_path.exists(), ".devcontainer/devcontainer.json must exist"

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg.get("image") == "mcr.microsoft.com/devcontainers/python:3.12"
    assert 8765 in cfg.get("forwardPorts", [])

    ports_attrs = cfg.get("portsAttributes", {})
    assert "8765" in ports_attrs
    assert "Nexus-Lite" in ports_attrs["8765"].get("label", "")

    post_cmd = cfg.get("postCreateCommand", "")
    assert "dtach" in post_cmd
    assert "pip install" in post_cmd

    settings = cfg.get("customizations", {}).get("vscode", {}).get("settings", {})
    assert settings.get("python.testing.pytestEnabled") is True


def test_protocol_primitives():
    """Verify sanitize_pointer_token, truncate_words, format_pointer_line, pointer_parts, and parse_pointer_line."""
    assert nl.POINTER_MAX_CHARS == 300
    assert protocol.sanitize_pointer_token("dm\x00 test\tval") == "dm_test_val"
    assert protocol.sanitize_pointer_token("   \x07 ") == "-"

    assert protocol.truncate_words("hello world", 20) == "hello world"
    assert protocol.truncate_words("hello world", 1) == "h"
    assert protocol.truncate_words("hello world foo", 10) == "hello…"
    assert protocol.truncate_words("helloworldfoo", 6) == "hello…"

    line = nl.format_pointer_line("dm", "m-1234", "/tmp/inbox/m-1234.md", "First line\nSecond line")
    assert line == "[nexus] dm m-1234 /tmp/inbox/m-1234.md - First line Second line"

    parsed = nl.parse_pointer_line(line)
    assert parsed == {
        "kind": "dm",
        "id": "m-1234",
        "path": "/tmp/inbox/m-1234.md",
        "summary": "First line Second line",
    }
    assert nl.pointer_parts(line) == ("m-1234", "/tmp/inbox/m-1234.md")

    assert nl.parse_pointer_line("not a pointer line") is None
    assert nl.pointer_parts("malformed line") == ("", "")

    long_summary = "word " * 100
    truncated_line = nl.format_pointer_line("task", "t-99", "/p/out.md", long_summary, limit=80)
    assert len(truncated_line) <= 80
    assert truncated_line.endswith("…")


def test_roles_tier_rank_and_tier_for_role():
    """Verify TIER_ORDER, tier_rank, and tier_for_role clamping."""
    assert nl.TIER_ORDER == ("haiku", "sonnet", "opus", "fable")
    assert nl.tier_rank("haiku") == 0
    assert nl.tier_rank("claude-haiku-5-5") == 0
    assert nl.tier_rank("sonnet") == 1
    assert nl.tier_rank(None) == 1
    assert nl.tier_rank("unknown-model") == 1
    assert nl.tier_rank("claude-opus-5-5") == 2
    assert nl.tier_rank("claude-fable-5-1") == 3

    assert nl.tier_for_role("driver") == "sonnet"
    assert nl.tier_for_role("navigator") == "opus"
    assert nl.tier_for_role(None, agent_name="mini-nexus") == "opus"
    assert nl.tier_for_role("verifier", producer_model="claude-haiku-5-5") == "sonnet"
    assert nl.tier_for_role("verifier", producer_model="claude-opus-5-5") == "opus"
    # Never auto-escalates verifier to fable
    assert nl.tier_for_role("verifier", producer_model="claude-fable-5-1") == "opus"


def test_pty_key_to_bytes_and_agent_normalization(monkeypatch):
    """Verify KEY_NAMES, key_to_bytes, keys_to_bytes, and normalize_agent_cmd for agy/claude/opencode."""
    assert "btab" in nl.KEY_NAMES
    assert "pgup" in nl.KEY_NAMES
    assert "c-u" in nl.KEY_NAMES
    assert "c-w" in nl.KEY_NAMES

    assert nl.key_to_bytes("Enter") == b"\r"
    assert nl.key_to_bytes("btab") == b"\x1b[Z"
    assert nl.key_to_bytes("c-a") == b"\x01"
    assert nl.key_to_bytes("ctrl-c") == b"\x03"
    assert nl.key_to_bytes("m-x") == b"\x1bx"
    assert nl.key_to_bytes("alt-f") == b"\x1bf"
    assert nl.key_to_bytes("hello") == b"hello"
    assert nl.keys_to_bytes(["c-a", "hi", "enter"]) == b"\x01hi\r"

    monkeypatch.setattr(pty, "find_opencode_bin", lambda: "/usr/local/bin/opencode")
    assert pty.normalize_agent_cmd("opencode", mode="pty") == "/usr/local/bin/opencode -m opencode/big-pickle"
    assert (
        pty.normalize_agent_cmd("opencode/mimo-v2.6-flash-free", mode="oneshot", prompt="fix bug")
        == "/usr/local/bin/opencode run --standalone --auto -m opencode/mimo-v2.6-flash-free 'fix bug'"
    )


def test_lazy_loading_extras():
    """Verify importing nexus_lite does not eagerly import bridge, demos, or viz until accessed."""
    code = (
        "import sys, nexus_lite as nl; "
        "assert 'nexus_lite.core' in sys.modules; "
        "assert 'nexus_lite.pty' in sys.modules; "
        "assert 'nexus_lite.roles' in sys.modules; "
        "assert 'nexus_lite.protocol' in sys.modules; "
        "assert 'nexus_lite.bridge' not in sys.modules; "
        "assert 'nexus_lite.demos' not in sys.modules; "
        "assert 'nexus_lite.viz' not in sys.modules; "
        "_ = nl.render_dashboard_html; "
        "assert 'nexus_lite.viz' in sys.modules; "
        "_ = nl.RemoteColab; "
        "assert 'nexus_lite.bridge' in sys.modules; "
        "_ = nl.run_all_demos; "
        "assert 'nexus_lite.demos' in sys.modules"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"Lazy loading check failed: {proc.stderr}"


def test_stm_and_escalation_integration(isolated_env):
    """Verify Exit Code 3 escalation handover and native file-backed STM (`stm/1`) operations."""
    import shlex

    # 1. Exit Code 3 (`escalation_required`) handling in delegate()
    esc_script = (
        f"{shlex.quote(sys.executable)} -c "
        "\"import json, sys; "
        "print(json.dumps({'t': 'escalation_required', 'reason': 'tier_exhausted', "
        "'attempts': 2, 'cloud_eligible': True, 'task_slice': {'minimal_context': 'cross-module'}})); "
        "sys.exit(3)\""
    )
    esc_res = nl.delegate(esc_script, "refactor module", caller="orch", task_id="t-esc-test")
    assert esc_res["rc"] == 3
    assert esc_res["status"] == "escalated"
    assert esc_res["escalation"]["reason"] == "tier_exhausted"
    msgs = nl.bus_read(target="orch")
    assert any("TASK_ESCALATE t-esc-test: tier_exhausted" in m["content"] for m in msgs)

    # 2. Native file-backed STM (`stm/1`) and pure-Python `<nexus_stm>` packing
    stm_nodes = nl.stm_list(workspace=isolated_env)
    assert len(stm_nodes) >= 1
    esc_stm_id = nl.stm.node_id_for_task("t-esc-test")
    esc_node = nl.stm_read(esc_stm_id, workspace=isolated_env)
    assert esc_node is not None
    assert esc_node["schema"] == "stm/1"
    assert esc_node["status"] == "needs-human"

    # Prove Files are the Source of Truth and SQLite `tasks` is rebuildable from `stm-<pid>/node/*.md`
    with nl.get_db() as con:
        con.execute("DELETE FROM tasks;")
        assert con.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] == 0
    rebuilt = nl.stm_rebuild_index(isolated_env)
    assert rebuilt >= 1
    with nl.get_db() as con:
        assert con.execute("SELECT COUNT(*) FROM tasks;").fetchone()[0] >= 1

    (isolated_env / "AGENTS.md").write_text("# Workspace Agents\nMaintain zero-dependency kernel.\n", encoding="utf-8")
    nl.stm_write(
        kind="decision",
        status="done",
        summary="Verified pure-Python stm/1 packing",
        agent="agy",
        workspace=isolated_env,
    )
    stm_json = nl.stm_pack(isolated_env, as_json=True)
    assert stm_json["project_id"].startswith("p") and len(stm_json["project_id"]) == 21
    assert stm_json.get("stm_available") is True
    assert stm_json.get("agents_md_path", "").endswith("AGENTS.md")
    assert any("pure-Python stm/1" in (n.get("summary") or "") for n in stm_json.get("recent_nodes", []))

    stm_fenced = nl.stm_pack(isolated_env, as_json=False)
    assert "<nexus_stm " in stm_fenced
    assert "pure-Python stm/1" in stm_fenced

    stm_phi = nl.stm_pack(isolated_env, phi=True, as_json=True)
    assert all(not n.get("summary") for n in stm_phi.get("recent_nodes", []))


def test_multi_tier_swarm_orchestration(isolated_env):
    """
    Verify 3-Tier Swarm Orchestration (`run_multi_tier_swarm`):
    - Tier 0 (scout) -> Tier 1 (`opencode` swarm) -> Tier 2 (`agy` verifier)
    - Strict PHI mode (`phi=True`) confining execution to Tier 0
    - Native `stm/1` file-backed STM nodes + LTM Wiki synthesis
    """
    import shlex

    py = shlex.quote(sys.executable)
    fake_t1 = [
        {
            "task_id": "t1-opencode-a",
            "cmd": f"{py} -c 'print(\"TIER1_OPENCODE_A_PROPOSAL_OK\")'",
            "prompt": "Analyze implementation option A",
        },
        {
            "task_id": "t1-opencode-b",
            "cmd": f"{py} -c 'print(\"TIER1_OPENCODE_B_PROPOSAL_OK\")'",
            "prompt": "Analyze implementation option B",
        },
    ]
    fake_t2 = f"{py} -c 'print(\"TIER2_AGY_VERIFIED_SYNTHESIS_OK\")'"

    # 1. Full 3-tier pipeline with Tier-0 (mock provider) + Tier-1 Swarm + Tier-2 Verifier
    res = nl.run_multi_tier_swarm(
        "Design and verify multi-tier swarm orchestration",
        workspace=isolated_env,
        tier0_provider="mock",
        stm=True,
        tier1_tasks=fake_t1,
        tier2_agent=fake_t2,
        wiki_slug="swarm/test-multi-tier",
    )
    assert len(res["tier0_results"]) == 1
    assert res["tier0_results"][0]["rc"] == 0
    assert "mock task complete" in res["tier0_results"][0]["output"]
    assert len(res["tier1_results"]) == 2
    assert any("TIER1_OPENCODE_A_PROPOSAL_OK" in r["output"] for r in res["tier1_results"])
    assert any("TIER1_OPENCODE_B_PROPOSAL_OK" in r["output"] for r in res["tier1_results"])
    assert res["tier2_result"] is not None
    assert "TIER2_AGY_VERIFIED_SYNTHESIS_OK" in res["tier2_result"]["output"]
    assert "TIER2_AGY_VERIFIED_SYNTHESIS_OK" in res["final_output"]
    assert res["wiki_path"] is not None

    wiki_doc = nl.wiki_read("swarm/test-multi-tier")
    assert "Tier 0 — Local Leaf Execution" in wiki_doc
    assert "Tier 1 — Parallel Cloud Swarm" in wiki_doc
    assert "Tier 2 — Navigator/Verifier Synthesis" in wiki_doc

    # Verify STM nodes were persisted as `stm/1` files on disk
    stm_nodes = nl.stm_list(workspace=isolated_env)
    assert len(stm_nodes) >= 4

    # 2. Strict PHI mode (`phi=True`) confines execution to Tier-0 and blocks Tier-1/Tier-2 cloud stages
    res_phi = nl.run_multi_tier_swarm(
        "Analyze confidential PHI cohort locally",
        workspace=isolated_env,
        phi=True,
        tier0_provider="mock",
        tier1_tasks=fake_t1,
        tier2_agent=fake_t2,
    )
    assert len(res_phi["tier0_results"]) == 1
    assert res_phi["tier0_results"][0]["rc"] == 0
    assert res_phi["tier1_results"] == []
    assert res_phi["tier2_result"] is None
    assert res_phi["phi"] is True

    # 3. Verify Agent-to-Agent (A2A) interaction and SVG peer-to-peer DM edges in dashboard
    a2a_res = nl.run_agent_interaction(
        "Implement sample helper in helper.py",
        target_file="helper.py",
        workspace=isolated_env,
        architect_agent=f"{py} -c 'print(\"Implement def add(a, b) in helper.py\")'",
        reviewer_agent=f"{py} -c 'print(\"Add type hints and None check to helper.py\")'",
        coder_provider="mock",
        wiki_slug="interaction/test-a2a",
    )
    assert len(a2a_res["turns"]) == 5
    assert len(a2a_res["dm_chain"]) == 5
    assert a2a_res["dm_chain"][0]["sender"] == "agy-architect"
    assert a2a_res["dm_chain"][0]["target"] == "code-driver"
    assert a2a_res["dm_chain"][1]["sender"] == "code-driver"
    assert a2a_res["dm_chain"][1]["target"] == "opencode-reviewer"
    assert a2a_res["dm_chain"][2]["sender"] == "opencode-reviewer"
    assert a2a_res["dm_chain"][2]["target"] == "code-driver"
    assert a2a_res["dm_chain"][3]["sender"] == "code-driver"
    assert a2a_res["dm_chain"][3]["target"] == "agy-architect"

    dash_html = nl.render_dashboard_html()
    assert "PEER AGENT" in dash_html
    assert "dm:m-" in dash_html
    assert "agy-architect" in dash_html
    assert "code-driver" in dash_html
    assert "opencode-reviewer" in dash_html


def test_cooperative_multi_agent_problem_solving(isolated_env):
    """
    Verify 5-Phase Multi-Agent Cooperative Problem-Solving Workflow (`run_cooperative_problem_solver`):
    - Phase 1: `thinker-architect` decomposes problem into 3 contracts, writes FTS5 Wiki spec + STM decision,
      and sends `[nexus] dm` pointers to `algo-driver` and `test-engineer`.
    - Phase 2: Parallel specialist swarm (`algo-driver` + `test-engineer`) creates `cohort_integrity_engine.py`
      and 6-test adversarial suite `test_cohort_integrity_engine.py`, then DMs `peer-verifier`.
    - Phase 3: `peer-verifier` runs `pytest` against Phase-2 draft (`draft_pytest_rc != 0`), catches 2 edge-case
      bugs (`±Inf` and case-insensitive leakage columns), and DMs critique back to `algo-driver`.
    - Phase 4: `algo-driver` (`refiner` role with `<nexus_stm>` continuity) reads critique DM, repairs
      `cohort_integrity_engine.py`, and sends `handoff` pointer to `peer-verifier`.
    - Phase 5: Final `pytest` verification passes 100% (`final_pytest_rc == 0`, `6 passed`), broadcasts
      `COOP_SOLVE_COMPLETE`, indexes full report in FTS5 Wiki, and renders HTML5 topology dashboard.
    """
    res = nl.run_cooperative_problem_solver(
        workspace=isolated_env,
        architect_agent="python",
        wiki_slug="coop/test_solution",
        render_dashboard=True,
    )

    assert len(res["phases"]) == 5
    assert res["draft_pytest_rc"] != 0, "Phase-2 draft must fail adversarial edge-case tests before peer review"
    assert res["final_pytest_rc"] == 0, f"Phase-5 repaired module must pass all tests: {res['final_pytest_output']}"
    assert "6 passed" in res["final_pytest_output"]
    assert "VERDICT: PASS" in res["consensus_verdict"]

    # Verify full inter-agent DM / handoff / broadcast pointer chain (7 messages across 4 peer agents)
    dm_chain = res["dm_chain"]
    assert len(dm_chain) == 7
    senders_targets = [(d["sender"], d["target"], d["kind"]) for d in dm_chain]
    assert senders_targets == [
        ("thinker-architect", "algo-driver", "dm"),
        ("thinker-architect", "test-engineer", "dm"),
        ("algo-driver", "peer-verifier", "dm"),
        ("test-engineer", "peer-verifier", "dm"),
        ("peer-verifier", "algo-driver", "dm"),
        ("algo-driver", "peer-verifier", "handoff"),
        ("thinker-architect", "ALL", "broadcast"),
    ]
    for d in dm_chain:
        parsed = nl.parse_pointer_line(d["pointer"])
        assert parsed is not None
        assert parsed["id"] == d["id"]

    # Verify FTS5 Wiki contains both architecture spec and final cooperative report
    spec_doc = nl.wiki_read("coop/architecture_spec")
    assert "Contract 1" in spec_doc and "Contract 2" in spec_doc and "Contract 3" in spec_doc
    sol_doc = nl.wiki_read("coop/test_solution")
    assert "6 passed" in sol_doc
    assert "VERDICT: PASS" in sol_doc
    hits = nl.wiki_search("pseudonymize_patient_id")
    assert any(h["slug"] == "coop/architecture_spec" for h in hits)

    # Verify STM nodes and dashboard HTML
    assert len(res["stm_nodes"]) >= 5
    assert res["dashboard_path"] and Path(res["dashboard_path"]).exists()
    dash_html = Path(res["dashboard_path"]).read_text(encoding="utf-8")
    assert "ORCHESTRATORS &amp; PEER AGENTS" in dash_html
    assert "thinker-architect" in dash_html
    assert "algo-driver" in dash_html
    assert "peer-verifier" in dash_html




# ---------------------------------------------------------------------------
# Process lifecycle: group kill, timeout status, durable bg, exit codes
# ---------------------------------------------------------------------------

def _pgrep_marker(marker: str) -> list[str]:
    out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    return [p for p in out.split() if p != str(os.getpid())]


def test_delegate_timeout_kills_process_group_and_returns_promptly():
    marker = f"sleep 47.{os.getpid()}"
    t0 = time.monotonic()
    res = core.delegate(f"sh -c '{marker} & wait'", "x", timeout=2)
    elapsed = time.monotonic() - t0
    assert elapsed < 4, f"delegate hung {elapsed:.1f}s on grandchild-held pipe"
    assert res["status"] == "timeout"
    time.sleep(0.3)
    assert _pgrep_marker(marker) == []


def test_pty_timeout_kills_session_and_marks_timeout(monkeypatch):
    killed = []
    monkeypatch.setattr(pty, "spawn", lambda *a, **k: None)
    monkeypatch.setattr(pty, "send", lambda *a, **k: None)
    monkeypatch.setattr(pty, "wait", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("idle")))
    monkeypatch.setattr(pty, "kill", lambda name: killed.append(name) or True)
    res = core.delegate("agy", "x", mode="pty", timeout=1, task_id="t-ptyto")
    assert res["status"] == "timeout"
    assert killed == ["worker-t-ptyto"]
    row = [t for t in core.list_tasks() if t["id"] == "t-ptyto"][0]
    assert row["status"] == "timeout"


def test_bg_delegate_reaches_terminal_status_via_detached_runner():
    res = core.delegate("echo hi", "x", bg=True, timeout=20)
    assert res["status"] == "running"
    deadline = time.monotonic() + 20
    row = None
    while time.monotonic() < deadline:
        row = [t for t in core.list_tasks() if t["id"] == res["task_id"]][0]
        if row["status"] != "running":
            break
        time.sleep(0.2)
    assert row["status"] == "completed"
    pid = int((Path(res["artifact_dir"]) / "runner.pid").read_text())
    assert pid != os.getpid()


def _insert_running(task_id, art, created):
    with core.get_db() as con:
        con.execute(
            "INSERT INTO tasks (id, caller, agent_cmd, status, rc, created_at, completed_at, artifact_dir)"
            " VALUES (?, 'main', 'x', 'running', NULL, ?, NULL, ?);",
            (task_id, created, str(art)),
        )


def test_stale_reaper_dead_pid_and_overdue(tmp_path):
    dead = subprocess.Popen(["true"]); dead.wait()
    a1 = tmp_path / "a1"; a1.mkdir()
    (a1 / "runner.pid").write_text(str(dead.pid))
    _insert_running("t-dead", a1, time.time())
    a2 = tmp_path / "a2"; a2.mkdir()
    (a2 / "runner.pid").write_text(str(os.getpid()))
    (a2 / "timeout").write_text("5")
    _insert_running("t-old", a2, time.time() - 1000)
    a3 = tmp_path / "a3"; a3.mkdir()
    (a3 / "runner.pid").write_text(str(os.getpid()))
    (a3 / "timeout").write_text("500")
    _insert_running("t-live", a3, time.time())
    st = {t["id"]: t["status"] for t in core.list_tasks()}
    assert st["t-dead"] == "failed"
    assert st["t-old"] == "timeout"
    assert st["t-live"] == "running"


def test_cli_delegate_exit_codes(capsys):
    assert cli.main(["delegate", "true", "x"]) == 0
    assert cli.main(["delegate", "false", "x"]) == 1
    assert cli.main(["delegate", "sleep 30", "x", "--timeout", "1"]) == 124
    assert cli.status_exit_code({"status": "escalated"}) == 3
    assert "124 timeout" in " ".join(cli.build_parser().format_help().split())


def test_cli_multi_tier_and_interact_propagate_failure(monkeypatch, capsys):
    monkeypatch.setattr(core, "run_multi_tier_swarm", lambda *a, **k: {"tier2_result": {"status": "failed"}})
    assert cli.main(["multi-tier", "goal"]) == 1
    monkeypatch.setattr(core, "run_agent_interaction", lambda *a, **k: {"turns": [{"result": {"status": "timeout"}}]})
    assert cli.main(["interact", "goal"]) == 124


def test_delegate_timeout_bounded_when_escaped_grandchild_holds_pipe():
    marker = f"sleep 31.{os.getpid()}"
    t0 = time.monotonic()
    try:
        res = core.delegate(f"sh -c 'setsid {marker} & wait'", "x", timeout=1)
        elapsed = time.monotonic() - t0
        assert res["status"] == "timeout"
        assert elapsed < 10, f"communicate() not bounded: {elapsed:.1f}s"
    finally:
        subprocess.run(["pkill", "-f", marker])
