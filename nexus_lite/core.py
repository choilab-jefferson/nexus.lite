"""
nexus_lite.core — Core primitives for nexus-lite.

Provides:
- Workspace & SQLite WAL DB initialization
- Inter-agent message bus (bus_send, bus_read) with live PTY pointer delivery
- Task delegation (delegate) with depth enforcement and TASK_COMPLETE notifications
- Local Markdown Wiki with SQLite FTS5 search (wiki_write, wiki_read, wiki_search, wiki_list)
- Conversational chat helper (chat)

Demos/orchestration scenarios live in nexus_lite.demos and Colab helpers in nexus_lite.colab;
the core never imports them (old core.* names resolve through a lazy DeprecationWarning shim).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import warnings
from pathlib import Path
from typing import Any, Optional, Union

from . import artifacts as artifacts_mod
from . import pty
from ._proc import reap_in_background
from ._db import connect, get_db, get_db_path, get_workspace, init
from .guard import current_delegation_depth, leaf_guard_error
from .memory import mtm as _mtm
from .memory import stm as stm_mod
from .phi import phi_gate_error
from .provider_errors import _QUOTA_OR_AUTH_MARKERS as _PE_MARKERS
from .provider_errors import is_quota_or_auth_failure
from .roles import ROLE_FOR_AGENT, ROLE_PROMPTS



_QUOTA_OR_AUTH_MARKERS = _PE_MARKERS
_is_quota_or_auth_failure = is_quota_or_auth_failure


def _kill_group_and_collect(proc: "subprocess.Popen", grace: float = 5.0) -> tuple[str, str]:
    """Kill the child's whole process group, then collect output with a bounded wait.

    The child was started with start_new_session=True, so its pid is the pgid.
    A grandchild that inherited the pipes cannot block us: communicate() is bounded.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        if sig == signal.SIGTERM:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
    try:
        out, err = proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        out, err = "", ""
        for f in (proc.stdout, proc.stderr):
            try:
                if f:
                    f.close()
            except Exception:
                pass
    return out or "", err or ""


def _agy_headless_argv(cmd: Union[str, list[str]], timeout: float) -> Optional[list[str]]:
    """Parse argv; for any `agy [flags]` command return a headless argv (prompt goes via stdin).

    Returns None when cmd is not agy, is an agy subcommand, or already carries its own prompt flag.
    """
    try:
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(x) for x in cmd]
    except ValueError:
        return None
    if not argv or os.path.basename(argv[0]) != "agy":
        return None
    if len(argv) > 1 and not argv[1].startswith("-"):
        return None
    if any(a in ("-p", "--print", "--prompt") or a.startswith(("-p=", "--print=", "--prompt=")) for a in argv):
        return None
    flags = {a.split("=", 1)[0] for a in argv if a.startswith("--")}
    out = list(argv)
    if "--dangerously-skip-permissions" not in flags:
        out.append("--dangerously-skip-permissions")
    if "--print-timeout" not in flags:
        out += ["--print-timeout", f"{max(1, int(timeout))}s"]
    if "--output-format" not in flags:
        out += ["--output-format", "text"]
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_TERMINAL_STATUSES = ("completed", "failed", "escalated", "timeout")
_STALE_GRACE_S = 30.0


def reap_stale_tasks() -> list[str]:
    """Mark 'running' rows whose runner is gone, or that outlived their timeout, as terminal.

    Runner pid and timeout live in <artifact_dir>/runner.pid and <artifact_dir>/timeout.
    Rows with no runner.pid are only reaped by age if a timeout file exists.
    Returns the reaped task ids.
    """
    reaped: list[str] = []
    now = time.time()
    with connect() as con:
        rows = con.execute(
            "SELECT id, created_at, artifact_dir FROM tasks WHERE status = 'running';"
        ).fetchall()
        for r in rows:
            art = Path(r["artifact_dir"] or "")
            pid = None
            tmo = None
            try:
                pid = int((art / "runner.pid").read_text().strip())
            except Exception:
                pass
            try:
                tmo = float((art / "timeout").read_text().strip())
            except Exception:
                pass
            created = r["created_at"] or now
            status = reason = None
            if tmo is not None and now > created + tmo + _STALE_GRACE_S:
                status, reason = "timeout", f"exceeded timeout {tmo}s without finishing"
            elif pid is not None and not _pid_alive(pid):
                status, reason = "failed", f"runner pid {pid} exited without recording a result"
            if status is None:
                continue
            rc = 124 if status == "timeout" else -1
            cur = con.execute(
                "UPDATE tasks SET status = ?, rc = ?, completed_at = ? WHERE id = ? AND status = 'running';",
                (status, rc, now, r["id"]),
            )
            if cur.rowcount:
                reaped.append(r["id"])
                try:
                    (art / "reaped.txt").write_text(f"{status}: {reason}\n", encoding="utf-8")
                except Exception:
                    pass
    return reaped


def list_tasks(limit: int = 50) -> list[dict]:
    """List recent tasks (newest first), reaping stale 'running' rows first."""
    init()
    reap_stale_tasks()
    with connect() as con:
        cur = con.execute(
            """
            SELECT id, caller, agent_cmd, status, rc, created_at, completed_at, artifact_dir
            FROM tasks ORDER BY created_at DESC LIMIT ?;
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def delegate(    agent_cmd: str,
    prompt: str,
    caller: str = "main",
    task_id: Optional[str] = None,
    mode: str = "oneshot",
    bg: bool = False,
    timeout: float = 120.0,
    fallback_cmd: Optional[str] = None,
    deadline: Optional[float] = None,
) -> dict:
    """
    Delegate a subtask to a worker (a leaf agent process; in `mode="pty"` it runs inside a
    dtach PTY session named `worker-<task_id>`, but the worker itself is not a "session").
    - Enforces the leaf guard (nexus_lite.guard: depth/role env)
    - Creates .nexus/artifacts/<task_id>/
    - Runs worker (in a dtach PTY session or subprocess)
    - Automatically retries in-place with `fallback_cmd` if `fallback_cmd` is provided and primary agent fails due to quota/weekly-limit/auth
    - Writes output.md and rc
    - Updates SQLite tasks table
    - Completion channel: sends a `TASK_COMPLETE <task_id>: ...` (or `TASK_ESCALATE <task_id>: ...`)
      message, kind "task", sender = task_id, to `caller` via bus_send
    """
    err = leaf_guard_error(os.path.basename(agent_cmd.split()[0]) if agent_cmd.split() else "")
    if err:
        raise PermissionError(err)
    err = phi_gate_error(agent_cmd)
    if err:
        raise PermissionError(err)
    if os.environ.get("NEXUS_SENSITIVITY") == "phi":
        fallback_cmd = None  # never fall back to a cloud CLI under PHI
    current_depth = current_delegation_depth() or 0

    init()
    ws = get_workspace()
    if not task_id:
        task_id = f"t-{uuid.uuid4().hex[:8]}"

    art_dir = artifacts_mod.ensure_task_dir(task_id, artifacts_mod.artifacts_root(ws))
    (art_dir / artifacts_mod.PROMPT).write_text(prompt, encoding="utf-8")
    (art_dir / "timeout").write_text(str(timeout), encoding="utf-8")
    (art_dir / "runner.pid").write_text(str(os.getpid()), encoding="utf-8")

    ts_start = time.time()
    cmd_text_for_stm = " ".join(str(x) for x in agent_cmd) if isinstance(agent_cmd, list) else str(agent_cmd)
    ag_tok = cmd_text_for_stm.split()[0] if cmd_text_for_stm.split() else "worker"
    ag_name = os.path.basename(ag_tok)[:32] or "worker"
    first_prompt_ln = next((ln.strip() for ln in str(prompt).splitlines() if ln.strip()), task_id)
    stm_node_id = stm_mod.node_id_for_task(task_id)
    try:
        stm_mod.write_node(
            ws,
            node_id=stm_node_id,
            kind="task",
            status="running",
            summary=f"{task_id}: {first_prompt_ln[:120]}",
            agent=ag_name,
            task_id=task_id,
            updated_ts=ts_start,
        )
    except Exception:
        pass

    with connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO tasks (id, caller, agent_cmd, status, rc, created_at, completed_at, artifact_dir)
            VALUES (?, ?, ?, 'running', NULL, ?, NULL, ?);
            """,
            (task_id, caller, agent_cmd, ts_start, str(art_dir)),
        )

    child_env = os.environ.copy()
    child_env["NEXUS_LITE_DIR"] = str(ws)
    child_env["NEXUS_LITE_HOME"] = str(ws)
    child_env["NEXUS_DELEGATION_DEPTH"] = str(current_depth + 1)
    child_env["NEXUS_TASK_ID"] = task_id
    child_env["NEXUS_CALLER_ID"] = caller
    artifacts_mod.export_env(child_env, art_dir)
    repo_root = str(Path(__file__).resolve().parent.parent)
    cur_pypath = child_env.get("PYTHONPATH", "")
    pypath_parts = [str(ws), repo_root]
    if cur_pypath:
        pypath_parts.append(cur_pypath)
    child_env["PYTHONPATH"] = ":".join(pypath_parts)

    # Ensure standard opencode install dir (~/.opencode/bin) is on PATH
    opencode_bin_dir = str(Path.home() / ".opencode" / "bin")
    cur_path = child_env.get("PATH", "")
    if os.path.isdir(opencode_bin_dir) and opencode_bin_dir not in cur_path.split(os.pathsep):
        cur_path = f"{opencode_bin_dir}{os.pathsep}{cur_path}" if cur_path else opencode_bin_dir
        child_env["PATH"] = cur_path

    if "NEXUS_STM_DIR" not in child_env:
        stm_dir_candidate = ws / "stm"
        if stm_dir_candidate.is_dir():
            child_env.setdefault("NEXUS_PROJECT_ID", _resolve_or_generate_project_id(ws))
            child_env.setdefault("NEXUS_STM_DIR", str(stm_dir_candidate))

    timed_out_flag = [False]

    chain_deadline = time.monotonic() + timeout
    if deadline is not None:
        chain_deadline = min(chain_deadline, deadline)

    def _exec_oneshot(cmd_to_run: Union[str, list[str]]) -> tuple[int, str, str]:
        timeout = max(0.1, chain_deadline - time.monotonic())
        agy_argv = _agy_headless_argv(cmd_to_run, timeout)
        norm_cmd = cmd_to_run if agy_argv else pty.normalize_agent_cmd(cmd_to_run, mode="oneshot", prompt=prompt)
        cmd_str = cmd_to_run if isinstance(cmd_to_run, str) else " ".join(str(x) for x in cmd_to_run)
        if agy_argv:
            run_cmd = agy_argv
            proc_input = prompt
        elif norm_cmd != cmd_to_run or cmd_str.strip() in (
            "claude",
            "claude --dangerously-skip-permissions",
        ):
            run_cmd = norm_cmd if isinstance(norm_cmd, str) else " ".join(norm_cmd)
            proc_input = None
        else:
            run_cmd = cmd_str
            proc_input = prompt

        proc = subprocess.Popen(
            run_cmd,
            shell=not isinstance(run_cmd, list),
            stdin=subprocess.DEVNULL if proc_input is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
            cwd=str(ws),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(input=proc_input, timeout=timeout)
            rc_val = proc.returncode
            if norm_cmd != cmd_to_run and rc_val == 0 and stdout and stdout.strip():
                out_val = stdout
            else:
                out_val = stdout + (f"\n{stderr}" if stderr else "")
            return rc_val, out_val, stderr or ""
        except subprocess.TimeoutExpired:
            timed_out_flag[0] = True
            stdout, stderr = _kill_group_and_collect(proc)
            out_val = f"TIMEOUT after {timeout}s\n" + (stdout or "") + (f"\n{stderr}" if stderr else "")
            return -1, out_val, stderr or ""

    def _worker():
        nonlocal ag_name
        resolved_agent_cmd = agent_cmd
        if mode == "pty":
            sess_name = f"worker-{task_id}"
            pty.spawn(agent_cmd, name=sess_name, cwd=str(ws), env=child_env)
            try:
                if "agy" in agent_cmd:
                    pty.wait(sess_name, provider="agy", timeout=min(20.0, timeout))
                elif "claude" in agent_cmd:
                    pty.wait(sess_name, provider="claude", timeout=min(20.0, timeout))
                elif "opencode" in agent_cmd:
                    pty.wait(sess_name, provider="opencode", timeout=min(20.0, timeout))
                pty.send(sess_name, prompt, enter=True)
                prov = "agy" if "agy" in agent_cmd else "claude" if "claude" in agent_cmd else "opencode" if "opencode" in agent_cmd else ""
                output = pty.wait(sess_name, provider=prov, timeout=timeout)
                rc = 0
            except TimeoutError as exc:
                timed_out_flag[0] = True
                rc = -1
                output = f"TIMEOUT after {timeout}s\n{exc}"
            finally:
                pty.kill(sess_name)
        else:
            rc, output, stderr = _exec_oneshot(agent_cmd)
            if stderr:
                (art_dir / "stderr.log").write_text(stderr, encoding="utf-8")
            if (
                fallback_cmd
                and fallback_cmd != agent_cmd
                and _is_quota_or_auth_failure(rc, output)
            ):
                (art_dir / "primary_failed.log").write_text(
                    f"cmd={agent_cmd}\nrc={rc}\n{output}",
                    encoding="utf-8",
                )
                timed_out_flag[0] = False
                fb_rc, fb_out, fb_err = _exec_oneshot(fallback_cmd)
                if not _is_quota_or_auth_failure(fb_rc, fb_out) or fb_rc == 0:
                    rc, output = fb_rc, fb_out
                    resolved_agent_cmd = f"{agent_cmd} -> {fallback_cmd}"
                    fb_tok = str(fallback_cmd).split()[0] if str(fallback_cmd).split() else ag_name
                    ag_name = os.path.basename(fb_tok)[:32] or ag_name
                    if fb_err:
                        (art_dir / "stderr.log").write_text(fb_err, encoding="utf-8")

        artifacts_mod.write_result(art_dir, output, rc)
        ts_done = time.time()

        esc_payload = None
        if rc == 3 and output.strip():
            for line in reversed(output.strip().splitlines()):
                line_s = line.strip()
                if line_s.startswith("{") and "escalation_required" in line_s:
                    try:
                        esc_payload = json.loads(line_s)
                        (art_dir / "escalation.json").write_text(
                            json.dumps(esc_payload, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        break
                    except Exception:
                        pass

        if timed_out_flag[0] and rc != 0:
            task_status = "timeout"
        elif rc == 0:
            task_status = "completed"
        elif rc == 3 and esc_payload is not None:
            task_status = "escalated"
        else:
            task_status = "failed"

        try:
            artifacts_mod.write_meta(art_dir, task_id=task_id, agent=ag_name, status=task_status,
                                     rc=rc, started=ts_start, finished=ts_done)
        except Exception:
            pass

        try:
            stm_mod.write_node(
                ws,
                node_id=stm_node_id,
                kind="task",
                status=task_status,
                summary=f"{task_id}: {first_prompt_ln[:120]}",
                agent=ag_name,
                task_id=task_id,
                updated_ts=ts_done,
            )
        except Exception:
            pass

        with connect() as con:
            con.execute(
                """
                UPDATE tasks
                SET agent_cmd = ?, status = ?, rc = ?, completed_at = ?
                WHERE id = ?;
                """,
                (resolved_agent_cmd, task_status, rc, ts_done, task_id),
            )

        if task_status == "escalated" and esc_payload is not None:
            reason = esc_payload.get("reason", "escalation_required")
            _mtm.bus_send(
                target=caller,
                message=f"TASK_ESCALATE {task_id}: {reason}",
                sender=task_id,
                kind="task",
            )
        else:
            summary_line = output.strip().splitlines()[0][:80] if output.strip() else f"rc={rc}"
            _mtm.bus_send(
                target=caller,
                message=f"TASK_COMPLETE {task_id}: {summary_line}",
                sender=task_id,
                kind="task",
            )
        return rc, output, task_status, esc_payload

    if bg:
        spec = {
            "agent_cmd": agent_cmd, "prompt": prompt, "caller": caller, "task_id": task_id,
            "mode": mode, "timeout": timeout, "fallback_cmd": fallback_cmd,
        }
        spec_path = art_dir / artifacts_mod.SPEC
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        runner_env = dict(child_env)
        runner_env["NEXUS_DELEGATION_DEPTH"] = str(current_depth)  # runner's delegate() re-adds the hop
        runner_log = open(art_dir / "runner.log", "ab")
        try:
            runner = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; from nexus_lite.core import _run_task_main; sys.exit(_run_task_main(sys.argv[1:]))",
                 "--run-task", str(spec_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=runner_log,
                env=runner_env,
                cwd=str(ws),
                start_new_session=True,
            )
        finally:
            runner_log.close()  # child holds its own dup
        reap_in_background(runner)  # detached on purpose: outlives us; thread only reaps if we stay alive
        (art_dir / "runner.pid").write_text(str(runner.pid), encoding="utf-8")
        return {
            "task_id": task_id,
            "caller": caller,
            "status": "running",
            "artifact_dir": str(art_dir),
        }
    else:
        rc, output, task_status, esc_payload = _worker()
        res_dict: dict[str, Any] = {
            "task_id": task_id,
            "caller": caller,
            "status": task_status,
            "rc": rc,
            "artifact_dir": str(art_dir),
            "output": output,
        }
        if esc_payload is not None:
            res_dict["escalation"] = esc_payload
        return res_dict


def run_swarm(
    specs: list[dict],
    caller: str = "orchestrator",
    timeout: float = 120.0,
    live_viz: bool = False,
    verbose: bool = False,
) -> list[dict]:
    """
    Dispatch multiple subtasks in parallel (bg=True) and wait for completion.
    Polls the SQLite tasks table until all tasks finish (or timeout).
    Prints live worker completion lines if verbose=True (live_viz is deprecated and ignored).
    Returns list of completed task dicts with output.
    """
    init()
    task_ids: list[str] = []
    if live_viz:
        warnings.warn(
            "run_swarm(live_viz=...) is deprecated and ignored: the core renders no dashboards; "
            "use nexus_lite.demos.viz.dashboard()",
            DeprecationWarning,
            stacklevel=2,
        )

    if any(str(s.get("cmd") or s.get("agent_cmd") or "").strip().startswith("opencode") for s in specs):
        ensure_opencode(install=True)

    for spec in specs:
        cmd = spec.get("cmd") or spec.get("agent_cmd")
        prompt = spec.get("prompt", "")
        tid = spec.get("task_id")
        mode = spec.get("mode", "oneshot")
        spec_caller = spec.get("caller", caller)
        fb_cmd = spec.get("fallback_cmd")
        res = delegate(
            agent_cmd=cmd,
            prompt=prompt,
            caller=spec_caller,
            task_id=tid,
            mode=mode,
            bg=True,
            timeout=timeout,
            fallback_cmd=fb_cmd,
        )
        task_ids.append(res["task_id"])

    swarm_t0 = time.monotonic()
    last_heartbeat = swarm_t0
    announced_done: set[str] = set()
    deadline = swarm_t0 + timeout
    while time.monotonic() < deadline:
        reap_stale_tasks()
        with connect() as con:
            placeholders = ",".join("?" for _ in task_ids)
            cur = con.execute(
                f"""
                SELECT id, caller, agent_cmd, status, rc, created_at, completed_at, artifact_dir
                FROM tasks
                WHERE id IN ({placeholders});
                """,
                task_ids,
            )
            rows = {r["id"]: dict(r) for r in cur.fetchall()}

        all_done = True
        pending_ids: list[str] = []
        for tid in task_ids:
            r = rows.get(tid)
            st = r.get("status") if r else None
            if st in _TERMINAL_STATUSES:
                if verbose and tid not in announced_done:
                    announced_done.add(tid)
                    dur = (
                        (float(r["completed_at"]) - float(r["created_at"]))
                        if r and r.get("completed_at") and r.get("created_at")
                        else (time.monotonic() - swarm_t0)
                    )
                    print(
                        f"    ✓ [swarm] {tid} ({r.get('agent_cmd')}) -> {st} (rc={r.get('rc')}, {dur:.1f}s)",
                        flush=True,
                    )
            else:
                all_done = False
                pending_ids.append(tid)

        if all_done:
            break

        now_m = time.monotonic()
        if verbose and (now_m - last_heartbeat) >= 15.0:
            last_heartbeat = now_m
            print(
                f"    ⏳ [swarm] waiting on {len(pending_ids)} worker(s): {', '.join(pending_ids)} ({now_m - swarm_t0:.0f}s elapsed)...",
                flush=True,
            )

        time.sleep(0.1)

    with connect() as con:
        placeholders = ",".join("?" for _ in task_ids)
        cur = con.execute(
            f"""
            SELECT id, caller, agent_cmd, status, rc, created_at, completed_at, artifact_dir
            FROM tasks
            WHERE id IN ({placeholders});
            """,
            task_ids,
        )
        final_rows = {r["id"]: dict(r) for r in cur.fetchall()}

    results: list[dict] = []
    for tid in task_ids:
        t = final_rows.get(tid, {"id": tid, "status": "timeout", "rc": -1})
        t["task_id"] = tid
        output = ""
        art_dir = t.get("artifact_dir")
        if art_dir:
            out_file = Path(art_dir) / "output.md"
            if out_file.exists():
                try:
                    output = out_file.read_text(encoding="utf-8")
                except Exception:
                    pass
        t["output"] = output
        results.append(t)

    return results


def run_pipeline(
    stages: list[dict],
    initial_prompt: str,
    caller: str = "orchestrator",
    timeout: float = 120.0,
) -> dict:
    """
    Execute stages sequentially (e.g. driver -> navigator -> verifier).
    Passes previous stage output into the next stage prompt.
    Sends HANDOFF messages on bus_send between stages.
    Returns {"stages": results, "final_output": last_output}.
    """
    init()
    results: list[dict] = []
    current_input = initial_prompt

    for i, stage in enumerate(stages):
        stage_name = stage.get("name") or f"stage-{i + 1}"
        cmd = stage.get("cmd") or stage.get("agent_cmd")
        mode = stage.get("mode", "oneshot")
        stage_timeout = stage.get("timeout", timeout)
        tid = stage.get("task_id")

        if "prompt" in stage:
            template = stage["prompt"]
            if "{prev_output}" in template:
                stage_prompt = template.replace("{prev_output}", current_input)
            elif "{input}" in template:
                stage_prompt = template.replace("{input}", current_input)
            elif current_input:
                stage_prompt = f"{template}\n\nPrevious Stage Output:\n{current_input}"
            else:
                stage_prompt = template
        else:
            stage_prompt = current_input

        # Open-Core Kernel Phase 1: wire role prompt stance
        role = stage.get("role")
        if not role:
            if stage_name in ROLE_PROMPTS:
                role = stage_name
            elif stage_name in ROLE_FOR_AGENT:
                role = ROLE_FOR_AGENT[stage_name]

        if role and role in ROLE_PROMPTS and not stage_prompt.startswith("[ROLE:"):
            role_text = ROLE_PROMPTS[role]
            stage_prompt = f"[ROLE: {role}]\n{role_text}\n\n[TASK]\n{stage_prompt}"

        res = delegate(
            agent_cmd=cmd,
            prompt=stage_prompt,
            caller=caller,
            task_id=tid,
            mode=mode,
            bg=False,
            timeout=stage_timeout,
            fallback_cmd=stage.get("fallback_cmd"),
        )
        res["stage"] = stage_name
        results.append(res)

        output = res.get("output", "")
        current_input = output

        # If not the last stage, send HANDOFF message
        if i < len(stages) - 1:
            next_stage_name = stages[i + 1].get("name") or f"stage-{i + 2}"
            summary = output.strip().splitlines()[0][:80] if output.strip() else "stage completed"
            _mtm.bus_send(
                target=caller,
                message=f"HANDOFF {stage_name} to {next_stage_name}: {summary}\n\nArtifact output:\n{output}",
                sender=stage_name,
                kind="handoff",
            )

        if res.get("rc", 0) != 0 and not stage.get("continue_on_error", False):
            break

    return {
        "stages": results,
        "final_output": current_input,
    }


def chat(name: str, message: str, provider: str = "", timeout: float = 60.0) -> str:
    """Send message to a session PTY, wait until idle, and return rendered output."""
    pty.send(name, message, enter=True)
    return pty.wait(name, provider=provider, timeout=timeout)



def _is_opencode_v2(bin_path: str) -> bool:
    """Return True if `bin_path --version` reports OpenCode major version >= 2 (e.g. `opencode v2.2.0`)."""
    try:
        proc = subprocess.run(
            [bin_path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        out = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
        import re as _re

        m = _re.search(r"\bv?(\d+)\.\d+", out)
        if m:
            return int(m.group(1)) >= 2
    except Exception:
        pass
    return False


def ensure_opencode(install: bool = True) -> Optional[str]:
    """
    Locate or install the `opencode` v2 CLI coding agent (which includes built-in
    zero-signup free models such as `opencode/big-pickle`, `opencode/mimo-v2.6-flash-free`,
    and `opencode/glm-5.3-flash-free` and supports `opencode run --standalone --auto`).

    Always installs/upgrades to OpenCode v2 via `https://opencode.ai/v2/install` if missing
    or if an older OpenCode v1 binary (e.g. from `npm install -g opencode-ai` or `opencode.ai/install`)
    is detected. Ensures `~/.opencode/bin` is prepended to `os.environ["PATH"]`.
    Returns the resolved path to `opencode`, or None if unavailable.
    """
    for cand_dir in (Path.home() / ".opencode" / "bin", Path("/root/.opencode/bin")):
        try:
            is_d = os.path.isdir(str(cand_dir))
        except OSError:
            is_d = False
        if is_d:
            d_str = str(cand_dir)
            cur_path = os.environ.get("PATH", "")
            parts = [p for p in cur_path.split(os.pathsep) if p and p != d_str]
            os.environ["PATH"] = os.pathsep.join([d_str] + parts)

    found = pty.find_opencode_bin()
    if not install:
        return found
    if found and _is_opencode_v2(found):
        return found

    if os.environ.get("NEXUS_LITE_ALLOW_INSTALL") != "1":
        sys.stderr.write(
            "opencode not found. Install it yourself (e.g. `npm install -g opencode-ai`) "
            "or set NEXUS_LITE_ALLOW_INSTALL=1 to allow nexus_lite to run npm.\n"
        )
        return None

    if shutil.which("npm"):
        try:
            subprocess.run(["npm", "install", "-g", "opencode-ai"], check=False)
        except Exception as e:
            sys.stderr.write(f"Warning: npm install opencode-ai failed: {e}\n")

    for cand_dir in (Path.home() / ".opencode" / "bin", Path("/root/.opencode/bin")):
        try:
            is_d = os.path.isdir(str(cand_dir))
        except OSError:
            is_d = False
        if is_d:
            d_str = str(cand_dir)
            cur_path = os.environ.get("PATH", "")
            parts = [p for p in cur_path.split(os.pathsep) if p and p != d_str]
            os.environ["PATH"] = os.pathsep.join([d_str] + parts)

    return pty.find_opencode_bin()


def _resolve_or_generate_project_id(target_ws: Path) -> str:
    """Resolve a 21-char `p<20>` Nexus project ID via `nexus_lite.memory.stm`."""
    return stm_mod.resolve_project_id(target_ws, persist=True)


def _run_task_main(argv: list[str]) -> int:
    """Detached runner entry (spawned by delegate(bg=True)): `--run-task <spec.json>`."""
    if len(argv) != 2 or argv[0] != "--run-task":
        sys.stderr.write("usage: python -m nexus_lite.core --run-task <spec.json>\n")
        return 2
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    delegate(bg=False, **spec)
    return 0


# --- Deprecated re-exports (one release) ---
# The memory tiers moved to nexus_lite.memory.*; demos/orchestration moved to nexus_lite.demos.*
# and the Colab helpers to nexus_lite.colab.env. The core never imports
# those modules at import time: this lazy hook is the only edge, and it warns.
_MOVED = {
    "bus_send": "mtm", "bus_read": "mtm",
    "wiki_write": "ltm", "wiki_read": "ltm", "wiki_search": "ltm", "wiki_list": "ltm", "_wiki_path": "ltm",
    "stm_write": "stm", "stm_read": "stm", "stm_list": "stm",
    "stm_rebuild_index": "stm", "stm_search": "stm", "stm_pack": "stm",
}
_MOVED_OUT = {
    **{n: "demos.orchestration" for n in (
        "run_multi_tier_swarm", "run_agent_interaction", "run_cooperative_problem_solver",
        "_run_live_cooperative_problem_solver", "_delegate_with_opencode_fallback",
        "_extract_python_code", "_infer_module_stem", "_phi_env", "_PHI_LOCAL_MODEL", "_run_pytest")},
    **{n: "colab.env" for n in (
        "colab_sync_to_drive", "colab_sync_from_drive", "_colab_sync", "COLAB_LOCAL_DIR",
        "COLAB_DRIVE_DIR", "_DRIVE_SYNC_DIRS", "sync_dotdir_to_drive", "ColabInitResult",
        "detect_env", "ensure_colab", "ensure_env")},
}


def __getattr__(name: str) -> Any:
    import importlib
    import warnings

    tier = _MOVED.get(name)
    if tier is not None:
        target = f"nexus_lite.memory.{tier}"
    elif name in _MOVED_OUT:
        target = f"nexus_lite.{_MOVED_OUT[name]}"
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"nexus_lite.core.{name} is deprecated; use {target}.{name}",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(target), name)
