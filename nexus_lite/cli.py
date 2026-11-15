"""
nexus_lite.cli — Command-line interface for nexus-lite.

Subcommands:
- init
- pty (new/list/read/send/keys/wait/kill/attach)
- bus (send/read)
- agent run (one command per bus message)
- delegate
- wiki (write/read/search/list)
- chat
- stm / compute

Demo and orchestration scenarios (demo, multi-tier, interact, cooperate, dashboard) live in
`nexus-lite-demo` (nexus_lite.demos.cli); the kernel CLI does not import them.
"""

from __future__ import annotations

import os
import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Optional

from . import cas, compute, core, pty
from .memory import ltm as mem_ltm
from .memory import mtm as mem_mtm
from .memory import stm as mem_stm


EXIT_OK, EXIT_FAILED, EXIT_ESCALATED, EXIT_TIMEOUT, EXIT_CONFIG = 0, 1, 3, 124, 78
_STATUS_EXIT = {"timeout": EXIT_TIMEOUT, "failed": EXIT_FAILED, "escalated": EXIT_ESCALATED}


EXIT_CODES_HELP = (
    "exit codes (delegate): "
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

    pty_sub.add_parser("ls", help="List PTY sessions: no, name, provider, age, attached, alive")

    p_pty_attach = pty_sub.add_parser(
        "attach", help="Attach this terminal to a session (no argument: numbered picker; detach with Ctrl-\\)")
    p_pty_attach.add_argument("name", nargs="?", help="Session number from `pty ls`, part of a session name, or a "
                                                      "socket path (omit for the picker)")
    p_pty_attach.add_argument("--ro", action="store_true", help="Watch only: never send input (q also detaches)")
    g_detach = p_pty_attach.add_mutually_exclusive_group()
    g_detach.add_argument("--detach-char", "-e", default=None,
                          help="Detach key as ^X or a single character (default: $NEXUS_PTY_DETACH, else ^\\)")
    g_detach.add_argument("--no-detach", "-E", action="store_true", help="Disable the detach key")

    # bus
    p_bus = sub.add_parser("bus", help="Message bus commands (MTM: flow carried across interruptions; transport = bus)")
    bus_sub = p_bus.add_subparsers(dest="bus_subcommand", help="Bus operations")

    p_bus_send = bus_sub.add_parser("send", help="Send a message (lite verb 'bus send' = nexus say/dm/relay) to an agent name or a live PTY session")
    p_bus_send.add_argument("target", help="Target agent name or PTY session name, or ALL (broadcast to live sessions)")
    p_bus_send.add_argument("message", help="Message body")
    p_bus_send.add_argument("--sender", default=None,
                            help="Sender identifier (default: $NEXUS_AGENT, the agent itself; else 'user')")
    p_bus_send.add_argument("--kind", default="dm", help="Message kind (dm, broadcast, task); stored as-is in the pointer line")

    p_bus_read = bus_sub.add_parser("read", help="Read messages from the bus")
    p_bus_read.add_argument("--target", default=None,
                            help="Filter by target name (default: $NEXUS_AGENT, the agent's own inbox; else all)")
    p_bus_read.add_argument("--limit", type=int, default=20, help="Max messages to return")
    p_bus_read.add_argument("--since-seq", type=int, default=None, help="Only messages with seq > N")
    p_bus_read.add_argument("--full", action="store_true", help="Also print each message's whole body")
    p_bus_read.add_argument("--unacked", action="store_true",
                            help="Only unacked for --target (each line shows seq=N; ack with `bus ack TARGET N`)")

    p_bus_ack = bus_sub.add_parser("ack", help="Ack messages up to seq for a target")
    p_bus_ack.add_argument("target")
    p_bus_ack.add_argument("seq", type=int)

    p_bus_reg = bus_sub.add_parser("register", help="Mark a session live (redelivers unacked)")
    p_bus_reg.add_argument("name")
    p_bus_unreg = bus_sub.add_parser("unregister", help="Mark a session not live")
    p_bus_unreg.add_argument("name")

    p_bus_prune = bus_sub.add_parser("prune", help="Delete old acked messages")
    p_bus_prune.add_argument("--max-age-s", type=float, required=True)
    p_bus_prune.add_argument("--keep-last", type=int, default=0)

    # delegate
    p_del = sub.add_parser(
        "delegate",
        help="Delegate a task to a worker (a leaf agent process, not a PTY session)",
        description="Delegate a task. Exit codes: 0 ok, 1 failed, 3 escalated, 124 timeout, 78 config error.",
    )
    p_del.add_argument("cmd", help="Worker command line")
    p_del.add_argument("prompt", help="Task prompt or instruction")
    p_del.add_argument("--caller", default="main", help="Caller name that receives the TASK_COMPLETE / TASK_ESCALATE completion message (default: main)")
    p_del.add_argument("--task-id", default=None, help="Explicit task ID")
    p_del.add_argument("--bg", action="store_true", help="Run task in background")
    p_del.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds")

    # tasks
    p_art = sub.add_parser("artifacts", help="Offline content-addressed artifact verbs (add, commit, status, diff)")
    art_sub = p_art.add_subparsers(dest="art_subcommand")
    for _n, _h in (("add", "Stage files into the CAS"), ("commit", "Write ARTIFACT_MANIFEST.json for staged + committed files"),
                   ("status", "Show staged / changed files vs the last manifest"), ("diff", "Show per-file sha changes vs the last manifest")):
        _p = art_sub.add_parser(_n, help=_h)
        _p.add_argument("--dir", default=".", help="Artifact directory (default: cwd)")
        if _n == "add":
            _p.add_argument("paths", nargs="*", help="Relative paths to stage (default: all)")

    sub.add_parser("tasks", help="List tasks (reaps stale 'running' rows first)")
    sub.add_parser("agent", help="agent run NAME -- COMMAND: run a command once per bus message (see `nexus-lite agent -h`)")
    sub.add_parser("ballot", help="ballot open/cast/status: agreement among agents done by code (see `nexus-lite ballot -h`)")
    sub.add_parser("job", help="Detached jobs: start/status/wait/check/tail/stop/list (see `nexus-lite job -h`)")

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

    # stm
    p_stm = sub.add_parser("stm", help="Manage file-backed stm/1 nodes (STM: live working state) and <nexus_stm> briefs")
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

    # loom (explicit released-binary install)
    p_loom = sub.add_parser("loom", help="loom agent harness helpers")
    loom_sub = p_loom.add_subparsers(dest="loom_subcommand", help="loom operations")
    p_loom_inst = loom_sub.add_parser("install", help="Download the pinned loom release binary, verify sha256, install")
    p_loom_inst.add_argument("--version", default=None, help="loom version (default: pinned LOOM_VERSION)")
    p_loom_inst.add_argument("--dest", default=None, help="Install path (default: ~/.local/bin/loom)")

    # compute (zero-preinstall ship-lite-and-compute for Colab & local sandbox)
    p_comp = sub.add_parser(
        "compute",
        help="Ship nexus-lite and execute computations on Google Colab or a local sandbox without pre-installation",
    )
    p_comp.add_argument(
        "target",
        choices=["colab", "local"],
        help="Compute target: 'colab' (Colab runtime) or 'local' (isolated sandbox)",
    )
    p_comp.add_argument("--python", "-c", default=None, help="Inline Python code to run (has `nexus_lite`/`nl`, `WORKSPACE`, `JOB_DIR`, `RESULT`)")
    p_comp.add_argument("--script", "-s", default=None, help="Path to local Python script file to ship and execute")
    p_comp.add_argument("--cmd", default=None, help="Shell command to execute remotely with PYTHONPATH pre-configured for nexus_lite")
    p_comp.add_argument("--lite", default=None, help="nexus-lite CLI arguments string (e.g. 'tasks')")
    p_comp.add_argument("--file", action="append", default=None, help="Extra file to stage into remote job dir (format: local_path or remote_name=local_path)")
    p_comp.add_argument("--collect", action="append", default=None, help="Remote relative file path to collect back in result JSON")
    p_comp.add_argument("--url", default=None, help="Colab tunnel URL (default: $NEXUS_COLAB_URL or .nexus/tunnel.json)")
    p_comp.add_argument("--token", default=None, help="Colab tunnel bearer token")
    p_comp.add_argument("--session", dest="colab_session", default=None, help="Colab CLI session name (runs via `colab exec`; no tunnel needed)")
    p_comp.add_argument("--ssh", dest="colab_ssh", default=None, help="Colab SSH host (default: $NEXUS_COLAB_SSH)")
    p_comp.add_argument("--bootstrap-out", default=None, help="Write a standalone self-extracting Colab .py bundle to this path")
    p_comp.add_argument("--start-tunnel", action="store_true", help="Start nexus_lite.colab.tunnel inside the remote Colab runtime")
    p_comp.add_argument("--timeout", type=float, default=300.0, help="Execution timeout in seconds (default: 300)")

    sub.add_parser("pc", help="Manage dedicated Colab Agent PC workstations (bind, ls, status, unbind, checkout, run, poll, pty, publish)")
    sub.add_parser("colab", help="Colab tunnel & Agent PC commands (start, stop, status, exec, pc)")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    eff_argv = list(sys.argv[1:] if argv is None else argv)
    if eff_argv and eff_argv[0] == "ballot":
        # agreement among agents done by code (own parser)
        return int(importlib.import_module(".ballot", __package__).cli_main(eff_argv[1:]) or 0)
    if eff_argv and eff_argv[0] == "job":
        # detached jobs have their own argument parser (the command after `--` is passed through untouched)
        return int(importlib.import_module(".jobs", __package__).cli_main(eff_argv[1:]) or 0)
    if eff_argv and eff_argv[0] == "agent":
        # agent run has its own parser: the command after `--` is passed through untouched
        return int(importlib.import_module(".agent_run", __package__).cli_main(eff_argv[1:]) or 0)
    if eff_argv and eff_argv[0] in ("pc", "colab"):
        # Lazy dispatch keeps the kernel import boundary: colab is loaded only on demand.
        tunnel_mod = importlib.import_module(".colab.tunnel", __package__)
        return int(tunnel_mod.main(eff_argv if eff_argv[0] == "pc" else eff_argv[1:]) or 0)
    parser = build_parser()
    args = parser.parse_args(eff_argv)

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

            elif args.pty_subcommand == "ls":
                from .pty import picker as _picker
                info = _picker.sessions_info()
                print(_picker.format_table(info) if info else "No active sessions.")
                return 0

            elif args.pty_subcommand == "attach":
                from .pty import cli as _ptycli
                from .pty import picker as _picker
                if args.no_detach:
                    detach = None
                else:
                    detach = _ptycli.parse_detach_char(args.detach_char) if args.detach_char else \
                        _picker.detach_from_env(b"\x1c")
                    if detach is None:
                        print(f"Error: invalid detach key {args.detach_char!r}", file=sys.stderr)
                        return 2
                return _picker.attach_command(args.name, detach, ro=args.ro)

        elif args.subcommand == "bus":
            if not args.bus_subcommand:
                parser.parse_args(["bus", "--help"])
                return 0

            if args.bus_subcommand == "send":
                sender = args.sender or os.environ.get("NEXUS_AGENT") or "user"  # an agent sends as itself
                res = mem_mtm.bus_send(args.target, args.message, sender=sender, kind=args.kind)
                print(f"Sent message {res['id']} to {res['target']}")
                return 0

            elif args.bus_subcommand == "read":
                msgs = mem_mtm.bus_read(
                    target=args.target or os.environ.get("NEXUS_AGENT") or None, limit=args.limit,
                    since_seq=args.since_seq, unacked=args.unacked
                )
                if not msgs:
                    print("No messages.")
                else:
                    for m in msgs:
                        # seq is what `bus ack <target> <seq>` takes; the summary is one line (100 chars), so the line
                        # also names the body file, as the PTY pointer does; --full prints the body itself
                        print(f"[{m['kind']}] seq={m['seq']} {m['id']} {m['sender']} -> {m['target']}: {m['summary']}"
                              f" (body: {m['path']})")
                        if args.full:
                            print(m.get("content") or "")
                            print()
                return 0

            elif args.bus_subcommand == "ack":
                print(f"acked {args.target} <= {mem_mtm.bus_ack(args.target, args.seq)}")
                return 0

            elif args.bus_subcommand == "register":
                res = mem_mtm.bus_register(args.name)
                print(f"registered {args.name}; redelivered {len(res['redelivered'])}")
                return 0

            elif args.bus_subcommand == "unregister":
                print(f"unregistered {args.name}" if mem_mtm.bus_unregister(args.name) else f"{args.name} not registered")
                return 0

            elif args.bus_subcommand == "prune":
                print(f"pruned {mem_mtm.bus_prune(args.max_age_s, args.keep_last)}")
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

        elif args.subcommand == "artifacts":
            if not args.art_subcommand:
                parser.parse_args(["artifacts", "--help"])
                return 0
            if args.art_subcommand == "add":
                print(json.dumps(cas.add(args.dir, args.paths), indent=2))
            elif args.art_subcommand == "commit":
                print(cas.commit(args.dir))
            elif args.art_subcommand == "status":
                print(json.dumps(cas.status(args.dir), indent=2))
            else:
                print(json.dumps(cas.diff(args.dir), indent=2))
            return 0

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
                p = mem_ltm.wiki_write(args.slug, content)
                print(f"Written wiki: {p}")
                return 0

            elif args.wiki_subcommand == "read":
                print(mem_ltm.wiki_read(args.slug))
                return 0

            elif args.wiki_subcommand == "search":
                results = mem_ltm.wiki_search(args.query)
                if not results:
                    print("No matches found.")
                else:
                    for r in results:
                        print(f"- {r['slug']}: {r.get('snippet', '')}")
                return 0

            elif args.wiki_subcommand == "list":
                docs = mem_ltm.wiki_list()
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

        elif args.subcommand == "stm":
            if not args.stm_subcommand:
                parser.parse_args(["stm", "--help"])
                return 0
            if args.stm_subcommand == "pack":
                out = mem_stm.stm_pack(
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
                nodes = mem_stm.stm_list(kind=args.kind, workspace=args.workspace)
                print(json.dumps(nodes, indent=2, ensure_ascii=False))
                return 0
            elif args.stm_subcommand == "search":
                hits = mem_stm.stm_search(args.query, limit=args.limit, workspace=args.workspace)
                print(json.dumps(hits, indent=2, ensure_ascii=False))
                return 0

        elif args.subcommand == "loom":
            if args.loom_subcommand != "install":
                parser.parse_args(["loom", "--help"])
                return 0
            from . import loom as _loom
            try:
                path = _loom.install_loom(args.version or _loom.LOOM_VERSION, dest=args.dest)
            except Exception as e:
                print(f"loom install failed: {e}", file=sys.stderr)
                return EXIT_FAILED
            print(path)
            return 0

        elif args.subcommand == "compute":
            import shlex as _shlex

            py_code = args.python
            if args.script:
                py_code = Path(args.script).expanduser().read_text(encoding="utf-8")
            lite_argv = _shlex.split(args.lite) if args.lite else None

            extra_files: dict[str, str] = {}
            for item in args.file or []:
                if "=" in item:
                    rname, lpath = item.split("=", 1)
                else:
                    lpath = item
                    rname = Path(item).name
                extra_files[rname] = Path(lpath).expanduser().read_text(encoding="utf-8")

            if args.target == "colab":
                res = compute.run_on_colab(
                    python=py_code,
                    cmd=args.cmd,
                    lite_argv=lite_argv,
                    files=extra_files or None,
                    collect_files=args.collect,
                    url=args.url,
                    token=args.token,
                    ssh_host=args.colab_ssh,
                    bootstrap_out=args.bootstrap_out,
                    start_tunnel=args.start_tunnel,
                    timeout=args.timeout,
                    colab_session=args.colab_session,
                )
            else:
                res = compute.run_local_sandbox(
                    python=py_code,
                    cmd=args.cmd,
                    lite_argv=lite_argv,
                    files=extra_files or None,
                    collect_files=args.collect,
                    timeout=args.timeout,
                )
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("ok", False) else EXIT_FAILED

    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return EXIT_CONFIG if isinstance(e, (PermissionError, FileNotFoundError)) else EXIT_FAILED

    return 0


if __name__ == "__main__":
    sys.exit(main())
