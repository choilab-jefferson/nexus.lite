"""
nexus_lite.cli — Command-line interface for nexus-lite.

Subcommands:
- init
- pty (new/list/read/send/keys/wait/kill)
- bus (send/read)
- delegate
- wiki (write/read/search/list)
- chat
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

from . import bridge, core, demos, pty, viz


EXIT_OK, EXIT_FAILED, EXIT_ESCALATED, EXIT_TIMEOUT, EXIT_CONFIG = 0, 1, 3, 124, 78
_STATUS_EXIT = {"timeout": EXIT_TIMEOUT, "failed": EXIT_FAILED, "escalated": EXIT_ESCALATED}


EXIT_CODES_HELP = (
    "exit codes (delegate, multi-tier, interact): "
    "0 ok, 1 failed, 3 escalated, 124 timeout, 78 config error"
)


def status_exit_code(*results) -> int:
    """Map task result dicts to a process exit code (timeout > failed > escalated > ok)."""
    codes = [_STATUS_EXIT.get(str((r or {}).get("status")), EXIT_OK) for r in results]
    for c in (EXIT_TIMEOUT, EXIT_FAILED, EXIT_ESCALATED):
        if c in codes:
            return c
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-lite",
        description="nexus-lite: ultra-simple Colab-ready multi-agent subsystem",
        epilog=EXIT_CODES_HELP,
    )
    sub = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # init
    p_init = sub.add_parser("init", help="Initialize workspace directory and SQLite database")
    p_init.add_argument("--workspace", "-w", help="Custom workspace path (default: .nexus/)")

    # pty
    p_pty = sub.add_parser("pty", help="PTY session controller commands")
    pty_sub = p_pty.add_subparsers(dest="pty_subcommand", help="PTY operations")

    p_pty_new = pty_sub.add_parser("new", help="Launch a new detached PTY session")
    p_pty_new.add_argument("cmd", help="Command to run in session")
    p_pty_new.add_argument("--name", "-n", help="Session name")
    p_pty_new.add_argument("--cwd", help="Working directory")

    pty_sub.add_parser("list", help="List running PTY sessions")

    p_pty_read = pty_sub.add_parser("read", help="Read rendered screen text from session")
    p_pty_read.add_argument("name", help="Session name or socket")
    p_pty_read.add_argument("--cols", type=int, default=160, help="Terminal columns (default: 160)")
    p_pty_read.add_argument("--rows", type=int, default=45, help="Terminal rows (default: 45)")
    p_pty_read.add_argument("--settle", type=float, default=0.25, help="Settle time in seconds (default: 0.25)")

    p_pty_send = pty_sub.add_parser("send", help="Send text/keystrokes to session")
    p_pty_send.add_argument("name", help="Session name")
    p_pty_send.add_argument("text", help="Text to inject")
    p_pty_send.add_argument("--no-enter", action="store_true", help="Do not send trailing Enter")

    p_pty_keys = pty_sub.add_parser("keys", help="Send specialized key tokens to session")
    p_pty_keys.add_argument("name", help="Session name")
    p_pty_keys.add_argument("tokens", nargs="+", help="Key tokens (e.g. Enter, c-c, Tab, Esc)")

    p_pty_wait = pty_sub.add_parser("wait", help="Wait for session to become idle")
    p_pty_wait.add_argument("name", help="Session name")
    p_pty_wait.add_argument("--provider", default="", help="Agent provider name (claude, agy, gemini, copilot, opencode, bash)")
    p_pty_wait.add_argument("--timeout", type=float, default=30.0, help="Timeout in seconds (default: 30.0)")

    p_pty_kill = pty_sub.add_parser("kill", help="Kill a session")
    p_pty_kill.add_argument("name", help="Session name")

    # bus
    p_bus = sub.add_parser("bus", help="Message bus commands")
    bus_sub = p_bus.add_subparsers(dest="bus_subcommand", help="Bus operations")

    p_bus_send = bus_sub.add_parser("send", help="Send a message to an agent/session")
    p_bus_send.add_argument("target", help="Target agent or session name (or ALL)")
    p_bus_send.add_argument("message", help="Message body")
    p_bus_send.add_argument("--sender", default="user", help="Sender identifier")
    p_bus_send.add_argument("--kind", default="dm", help="Message kind (dm, broadcast, task)")

    p_bus_read = bus_sub.add_parser("read", help="Read messages from the bus")
    p_bus_read.add_argument("--target", default=None, help="Filter by target name")
    p_bus_read.add_argument("--limit", type=int, default=20, help="Max messages to return")

    # delegate
    p_del = sub.add_parser(
        "delegate",
        help="Delegate a task to a worker agent",
        description="Delegate a task. Exit codes: 0 ok, 1 failed, 3 escalated, 124 timeout, 78 config error.",
    )
    p_del.add_argument("cmd", help="Worker command line")
    p_del.add_argument("prompt", help="Task prompt or instruction")
    p_del.add_argument("--caller", default="main", help="Caller session ID (default: main)")
    p_del.add_argument("--task-id", default=None, help="Explicit task ID")
    p_del.add_argument("--bg", action="store_true", help="Run task in background")
    p_del.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds")

    # tasks
    sub.add_parser("tasks", help="List tasks (reaps stale 'running' rows first)")

    # wiki
    p_wiki = sub.add_parser("wiki", help="Local markdown wiki commands")
    wiki_sub = p_wiki.add_subparsers(dest="wiki_subcommand", help="Wiki operations")

    p_wiki_write = wiki_sub.add_parser("write", help="Write a markdown document to the wiki")
    p_wiki_write.add_argument("slug", help="Wiki slug (relative path without .md)")
    p_wiki_write.add_argument("content", help="Markdown content (or '-' to read from stdin)")

    p_wiki_read = wiki_sub.add_parser("read", help="Read a wiki document")
    p_wiki_read.add_argument("slug", help="Wiki slug")

    p_wiki_search = wiki_sub.add_parser("search", help="Full-text search wiki articles")
    p_wiki_search.add_argument("query", help="Search query")

    wiki_sub.add_parser("list", help="List all wiki articles")

    # chat
    p_chat = sub.add_parser("chat", help="Send message to PTY agent and wait for response")
    p_chat.add_argument("name", help="Session name")
    p_chat.add_argument("message", help="Message prompt")
    p_chat.add_argument("--provider", default="", help="Agent provider")
    p_chat.add_argument("--timeout", type=float, default=60.0, help="Timeout in seconds")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Render HTML5 multi-agent visualization dashboard")
    p_dash.add_argument("--out", "-o", default=None, help="Output HTML file path (default: .nexus/dashboard.html)")

    # bridge
    p_bridge = sub.add_parser("bridge", help="Cloudflared remote Colab bridge server and client")
    bridge_sub = p_bridge.add_subparsers(dest="bridge_subcommand", help="Bridge operations")

    p_br_start = bridge_sub.add_parser("start", help="Start HTTP bridge server (loopback; tunnel only with --tunnel --token)")
    p_br_start.add_argument("--port", "-p", type=int, default=8765, help="Port to listen on (default: 8765)")
    p_br_start.add_argument("--tunnel", action="store_true", help="Publish a public trycloudflare tunnel (requires --token)")
    p_br_start.add_argument("--token", "-t", default=None, help="Bearer token (random if omitted; required with --tunnel)")
    p_br_start.add_argument("--show-token", action="store_true", help="Print the bearer token to stdout")

    bridge_sub.add_parser("stop", help="Stop running bridge server and tunnel")
    bridge_sub.add_parser("status", help="Show bridge server and tunnel status")

    p_br_exec = bridge_sub.add_parser("exec", help="Execute remote command on bridge server")
    p_br_exec.add_argument("cmd", help="Shell command to execute remotely")
    p_br_exec.add_argument("--url", "-u", required=True, help="Remote bridge URL")
    p_br_exec.add_argument("--token", "-t", required=True, help="Authentication Bearer token")

    # demo
    p_demo = sub.add_parser("demo", help="Run practical qradiomics + medimage multi-agent demos")
    p_demo.add_argument(
        "workflow",
        nargs="?",
        default="all",
        choices=[
            "all",
            "phantoms", "phantom-swarm",
            "privacy", "hipaa-pipeline",
            "multimodal", "multimodal-swarm",
            "lung1", "lung1-audit",
            "opencode", "opencode-free",
        ],
        help="Demo workflow to run (default: all)",
    )
    p_demo.add_argument(
        "--agent",
        "-a",
        default="auto",
        help="Agent CLI to orchestrate in swarms and role pipelines (auto, agy, opencode, claude, multi-tier, python)",
    )
    p_demo.add_argument("--work-dir", default=None, help="Custom work directory for demo artifacts")

    # multi-tier
    p_mt = sub.add_parser(
        "multi-tier",
        help="Run 3-Tier Swarm Orchestration (Tier-0 Scout -> Tier-1 OpenCode Swarm -> Tier-2 AGY Verifier)",
    )
    p_mt.add_argument("goal", help="High-level engineering or research goal")
    p_mt.add_argument("--workspace", "-w", default=None, help="Target workspace directory")
    p_mt.add_argument("--tier0-provider", default="opencode", help="Tier-0 scout provider (default: opencode)")
    p_mt.add_argument("--tier0-model", default="opencode/big-pickle", help="Tier-0 scout model (default: opencode/big-pickle)")
    p_mt.add_argument(
        "--tier1-agent",
        action="append",
        default=None,
        help="Tier-1 parallel swarm agent (repeatable, default: opencode/mimo-v2.6-flash-free + opencode/glm-5.3-flash-free)",
    )
    p_mt.add_argument("--tier2-agent", default="agy", help="Tier-2 verifier/synthesizer agent (default: agy)")
    p_mt.add_argument("--phi", action="store_true", help="Enforce PHI local-only confinement (Tier-0 only)")
    p_mt.add_argument("--stm", action="store_true", help="Opt in to <nexus_stm> short-term memory injection")
    p_mt.add_argument("--wiki-slug", default="multi_tier/latest_report", help="Wiki slug to store final report")
    p_mt.add_argument("--timeout", type=float, default=90.0, help="Per-tier timeout in seconds")

    # interact
    p_mt.epilog = EXIT_CODES_HELP
    p_int = sub.add_parser(
        "interact",
        help="Run a 5-turn Agent-to-Agent (A2A) interaction loop across AGY <-> OpenCode over [nexus] dm",
    )
    p_int.add_argument("goal", help="Engineering goal for collaborative implementation & peer review")
    p_int.add_argument("--target-file", default="solution.py", help="Target workspace file to write/revise")
    p_int.add_argument("--workspace", "-w", default=None, help="Target workspace directory")
    p_int.add_argument("--architect-agent", default="agy", help="Architect/Verifier agent (default: agy)")
    p_int.add_argument("--reviewer-agent", default="opencode/big-pickle", help="Peer reviewer agent (default: opencode/big-pickle)")
    p_int.add_argument("--coder-provider", default="opencode", help="Coder provider (default: opencode)")
    p_int.add_argument("--coder-model", default="", help="Coder model identifier")
    p_int.add_argument("--wiki-slug", default="interaction/latest_a2a", help="Wiki slug to store A2A transcript")
    p_int.add_argument("--timeout", type=float, default=90.0, help="Per-turn timeout in seconds")

    # cooperate
    p_coop = sub.add_parser(
        "cooperate",
        help="Run a 5-phase Multi-Agent Cooperative Problem-Solving Workflow (Architect -> Swarm -> Peer Verifier -> Refiner -> Consensus)",
    )
    p_coop.add_argument(
        "goal",
        nargs="?",
        default=(
            "Design, implement, adversarially test, and verify a leakage-safe "
            "Clinical Radiomics Cohort Stratification & HIPAA Pseudonymization Engine"
        ),
        help="Complex multi-constraint problem goal",
    )
    p_coop.add_argument("--workspace", "-w", default=None, help="Target workspace directory")
    p_coop.add_argument("--module-name", "-m", default=None, help="Target Python module name (auto-inferred if omitted)")
    p_coop.add_argument("--max-rounds", type=int, default=3, help="Max closed-loop repair iterations (default: 3)")
    p_coop.add_argument("--architect-agent", default="agy", help="Architect/Sign-off agent (default: agy, or python)")
    p_coop.add_argument("--verifier-agent", default="opencode/mimo-v2.6-flash-free", help="Test engineer & peer verifier agent")
    p_coop.add_argument("--coder-provider", default="opencode", help="Coder provider (default: opencode)")
    p_coop.add_argument("--coder-model", default="", help="Coder model identifier")
    p_coop.add_argument("--wiki-slug", default="coop/latest_solution", help="Wiki slug for final report")
    p_coop.add_argument("--timeout", type=float, default=90.0, help="Per-phase timeout in seconds")

    # stm
    p_stm = sub.add_parser("stm", help="Manage file-backed stm/1 short-term memory nodes and <nexus_stm> briefs")
    stm_sub = p_stm.add_subparsers(dest="stm_subcommand", help="STM operations")

    p_stm_pack = stm_sub.add_parser("pack", help="Pack workspace stm/1 brief (<nexus_stm> or JSON)")
    p_stm_pack.add_argument("--workspace", "-w", default=None, help="Target workspace directory")
    p_stm_pack.add_argument("--phi", action="store_true", help="Redact node summaries under PHI policy")
    p_stm_pack.add_argument("--json", action="store_true", help="Emit STM brief as JSON instead of <nexus_stm> fence")

    p_stm_list = stm_sub.add_parser("list", help="List stm/1 nodes in the workspace")
    p_stm_list.add_argument("--workspace", "-w", default=None, help="Target workspace directory")
    p_stm_list.add_argument("--kind", default=None, help="Filter by node kind (project|decision|task|session)")

    p_stm_search = stm_sub.add_parser("search", help="Search stm/1 nodes by keyword")
    p_stm_search.add_argument("query", help="Search query")
    p_stm_search.add_argument("--workspace", "-w", default=None, help="Target workspace directory")
    p_stm_search.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 0

    try:
        if args.subcommand == "init":
            ws = core.init(args.workspace)
            print(f"Initialized workspace at {ws}")
            return 0

        elif args.subcommand == "pty":
            if not args.pty_subcommand:
                parser.parse_args(["pty", "--help"])
                return 0

            if args.pty_subcommand == "new":
                name = pty.spawn(args.cmd, name=args.name, cwd=args.cwd)
                print(f"Spawned session: {name} (socket: {pty.sock_path(name)})")
                return 0

            elif args.pty_subcommand == "list":
                sessions = pty.list_sessions()
                if not sessions:
                    print("No active sessions.")
                else:
                    for s in sessions:
                        print(f"- {s['name']} -> {s['sock']}")
                return 0

            elif args.pty_subcommand == "read":
                screen = pty.read(args.name, cols=args.cols, rows=args.rows, settle=args.settle)
                sys.stdout.write(screen + "\n" if screen else "")
                return 0

            elif args.pty_subcommand == "send":
                pty.send(args.name, args.text, enter=not args.no_enter)
                print(f"Sent to {args.name}")
                return 0

            elif args.pty_subcommand == "keys":
                pty.keys(args.name, *args.tokens)
                print(f"Sent keys {args.tokens} to {args.name}")
                return 0

            elif args.pty_subcommand == "wait":
                screen = pty.wait(args.name, provider=args.provider, timeout=args.timeout)
                sys.stdout.write(screen + "\n" if screen else "")
                return 0

            elif args.pty_subcommand == "kill":
                pty.kill(args.name)
                print(f"Killed session {args.name}")
                return 0

        elif args.subcommand == "bus":
            if not args.bus_subcommand:
                parser.parse_args(["bus", "--help"])
                return 0

            if args.bus_subcommand == "send":
                res = core.bus_send(args.target, args.message, sender=args.sender, kind=args.kind)
                print(f"Sent message {res['id']} to {res['target']}")
                return 0

            elif args.bus_subcommand == "read":
                msgs = core.bus_read(target=args.target, limit=args.limit)
                if not msgs:
                    print("No messages.")
                else:
                    for m in msgs:
                        print(f"[{m['kind']}] {m['id']} {m['sender']} -> {m['target']}: {m['summary']}")
                return 0

        elif args.subcommand == "delegate":
            res = core.delegate(
                args.cmd,
                args.prompt,
                caller=args.caller,
                task_id=args.task_id,
                bg=args.bg,
                timeout=args.timeout,
            )
            print(json.dumps(res, indent=2))
            return status_exit_code(res)

        elif args.subcommand == "tasks":
            print(json.dumps(core.list_tasks(), indent=2))
            return 0

        elif args.subcommand == "wiki":
            if not args.wiki_subcommand:
                parser.parse_args(["wiki", "--help"])
                return 0

            if args.wiki_subcommand == "write":
                content = args.content
                if content == "-":
                    content = sys.stdin.read()
                p = core.wiki_write(args.slug, content)
                print(f"Written wiki: {p}")
                return 0

            elif args.wiki_subcommand == "read":
                print(core.wiki_read(args.slug))
                return 0

            elif args.wiki_subcommand == "search":
                results = core.wiki_search(args.query)
                if not results:
                    print("No matches found.")
                else:
                    for r in results:
                        print(f"- {r['slug']}: {r.get('snippet', '')}")
                return 0

            elif args.wiki_subcommand == "list":
                docs = core.wiki_list()
                if not docs:
                    print("No wiki pages.")
                else:
                    for d in docs:
                        print(f"- {d['slug']} ({d['size']} bytes)")
                return 0

        elif args.subcommand == "chat":
            output = core.chat(args.name, args.message, provider=args.provider, timeout=args.timeout)
            sys.stdout.write(output + "\n" if output else "")
            return 0

        elif args.subcommand == "dashboard":
            out_target = args.out if args.out else str(core.get_workspace() / "dashboard.html")
            viz.dashboard(out_path=args.out, display_inline=False)
            print(f"Dashboard rendered to {out_target}")
            return 0

        elif args.subcommand == "bridge":
            if not args.bridge_subcommand:
                parser.parse_args(["bridge", "--help"])
                return 0

            if args.bridge_subcommand == "start":
                info = bridge.start_bridge(port=args.port, token=args.token, tunnel=args.tunnel)
                print(f"Bridge started on port {info['port']}")
                print(f"Local URL: {info['local_url']}")
                if info.get("public_url"):
                    print(f"Public URL: {info['public_url']}")
                print(f"Dashboard URL: {info['dashboard_url']}")
                if args.show_token:
                    print(f"Token: {info['token']}")
                else:
                    print(f"Token file: {bridge.token_file_path(info['port'])} (0600)")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    bridge.stop_bridge()
                    print("Bridge stopped.")
                return 0

            elif args.bridge_subcommand == "stop":
                bridge.stop_bridge()
                print("Bridge stopped.")
                return 0

            elif args.bridge_subcommand == "status":
                info = bridge.get_bridge_info()
                if not info:
                    print("Bridge is not running.")
                else:
                    print(json.dumps(info, indent=2))
                return 0

            elif args.bridge_subcommand == "exec":
                client = bridge.RemoteColab(args.url, args.token)
                res = client.exec(cmd=args.cmd)
                if res.get("stdout"):
                    sys.stdout.write(res["stdout"])
                if res.get("stderr"):
                    sys.stderr.write(res["stderr"])
                return res.get("rc", 0)

        elif args.subcommand == "multi-tier":
            res = core.run_multi_tier_swarm(
                args.goal,
                workspace=args.workspace,
                tier0_provider=args.tier0_provider,
                tier0_model=args.tier0_model,
                tier1_agents=args.tier1_agent,
                tier2_agent=args.tier2_agent,
                phi=args.phi,
                stm=args.stm,
                wiki_slug=args.wiki_slug,
                timeout=args.timeout,
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))
            if res.get("tier2_result"):
                return status_exit_code(res["tier2_result"])
            return status_exit_code(*res.get("tier0_results", []))

        elif args.subcommand == "interact":
            res = core.run_agent_interaction(
                args.goal,
                target_file=args.target_file,
                workspace=args.workspace,
                architect_agent=args.architect_agent,
                reviewer_agent=args.reviewer_agent,
                coder_provider=args.coder_provider,
                coder_model=args.coder_model,
                wiki_slug=args.wiki_slug,
                timeout=args.timeout,
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return status_exit_code(*(t.get("result") for t in res.get("turns", [])[-1:]))

        elif args.subcommand == "cooperate":
            res = core.run_cooperative_problem_solver(
                args.goal,
                module_name=args.module_name,
                max_rounds=args.max_rounds,
                workspace=args.workspace,
                architect_agent=args.architect_agent,
                verifier_agent=args.verifier_agent,
                coder_provider=args.coder_provider,
                coder_model=args.coder_model,
                wiki_slug=args.wiki_slug,
                timeout=args.timeout,
                verbose=False,
            )
            print(json.dumps({
                "goal": res["goal"],
                "project_id": res["project_id"],
                "module_path": res["module_path"],
                "test_path": res["test_path"],
                "draft_pytest_rc": res["draft_pytest_rc"],
                "final_pytest_rc": res["final_pytest_rc"],
                "repair_rounds": [
                    {"round": r["round"], "agent": r["agent"], "pytest_rc": r["pytest_rc"]}
                    for r in res.get("repair_rounds", [])
                ],
                "consensus_verdict": res["consensus_verdict"],
                "wiki_path": res["wiki_path"],
                "dashboard_path": res["dashboard_path"],
            }, indent=2, ensure_ascii=False))
            return 0 if res.get("final_pytest_rc", 1) == 0 else 1

        elif args.subcommand == "stm":
            if not args.stm_subcommand:
                parser.parse_args(["stm", "--help"])
                return 0
            if args.stm_subcommand == "pack":
                out = core.stm_pack(
                    workspace=args.workspace,
                    phi=args.phi,
                    as_json=args.json,
                )
                if isinstance(out, dict):
                    print(json.dumps(out, indent=2, ensure_ascii=False))
                else:
                    print(out)
                return 0
            elif args.stm_subcommand == "list":
                nodes = core.stm_list(kind=args.kind, workspace=args.workspace)
                print(json.dumps(nodes, indent=2, ensure_ascii=False))
                return 0
            elif args.stm_subcommand == "search":
                hits = core.stm_search(args.query, limit=args.limit, workspace=args.workspace)
                print(json.dumps(hits, indent=2, ensure_ascii=False))
                return 0

        elif args.subcommand == "demo":
            wf = args.workflow
            ag = args.agent
            if wf in ("phantoms", "phantom-swarm"):
                res = demos.run_phantom_shape_swarm(work_dir=args.work_dir, agent=ag)
                print(json.dumps({
                    "agent_used": res["agent_used"],
                    "spic6": res["spic6"],
                    "ahsn": res["ahsn"],
                    "agent_outputs": res.get("agent_outputs", []),
                }, indent=2, ensure_ascii=False))
            elif wf in ("privacy", "hipaa-pipeline"):
                res = demos.run_hipaa_privacy_pipeline(work_dir=args.work_dir, agent=ag)
                print(json.dumps(res["audit"], indent=2, ensure_ascii=False))
            elif wf in ("multimodal", "multimodal-swarm"):
                res = demos.run_multimodal_imaging_swarm(work_dir=args.work_dir, agent=ag)
                print(json.dumps({
                    "agent_used": res["agent_used"],
                    "ct": res["ct_bodycomp"],
                    "mri": res["mri_steatosis"],
                    "pet": res["pet_suv"],
                    "agent_outputs": res.get("agent_outputs", []),
                }, indent=2, ensure_ascii=False))
            elif wf in ("lung1", "lung1-audit"):
                res = demos.run_lung1_audit_pipeline(work_dir=args.work_dir, agent=ag)
                print(json.dumps(res["review"], indent=2, ensure_ascii=False))
            elif wf in ("opencode", "opencode-free"):
                res = demos.run_opencode_free_demo(work_dir=args.work_dir)
                print(json.dumps({
                    "agent_used": res["agent_used"],
                    "models": res["models"],
                    "swarm": [
                        {"task_id": r["task_id"], "cmd": r["agent_cmd"], "status": r["status"], "output": r["output"].strip()}
                        for r in res["swarm"]
                    ],
                    "pipeline_final": res["pipeline"].get("final_output", "").strip(),
                    "wiki_slug": res["wiki_slug"],
                }, indent=2, ensure_ascii=False))
            else:
                res = demos.run_all_demos(work_dir=args.work_dir, agent=ag, render_dashboard=True)
                print(f"Completed all 4 qradiomics + medimage multi-agent demos (agent={res.get('agent_used')}).")
                print(f"Wiki articles indexed: {len(res['wiki_articles'])}")
                print(f"Dashboard: {res['dashboard_path']}")
            return 0

    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return EXIT_CONFIG if isinstance(e, (PermissionError, FileNotFoundError)) else EXIT_FAILED

    return 0


if __name__ == "__main__":
    sys.exit(main())
