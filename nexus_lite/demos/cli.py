"""
nexus_lite.demos.cli — `nexus-lite-demo`: demo and orchestration scenarios.

Subcommands moved out of the kernel CLI: demo, multi-tier, interact,
cooperate, dashboard. Depends on the kernel only; the kernel never imports this module.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .. import core
from ..cli import EXIT_CONFIG, EXIT_FAILED, status_exit_code
from . import orchestration, scenarios, viz


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-lite-demo",
        description="nexus-lite demos: qradiomics/medimage workflows, multi-tier swarm, A2A interaction, cooperative solver, dashboard",
        epilog="exit codes (multi-tier, interact): 0 ok, 1 failed, 3 escalated, 124 timeout, 78 config error",
    )
    sub = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Render HTML5 multi-agent visualization dashboard")
    p_dash.add_argument("--out", "-o", default=None, help="Output HTML file path (default: .nexus/dashboard.html)")

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

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 0

    try:
        if args.subcommand == "dashboard":
            out_target = args.out if args.out else str(core.get_workspace() / "dashboard.html")
            viz.dashboard(out_path=args.out, display_inline=False)
            print(f"Dashboard rendered to {out_target}")
            return 0

        elif args.subcommand == "multi-tier":
            res = orchestration.run_multi_tier_swarm(
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
            res = orchestration.run_agent_interaction(
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
            res = orchestration.run_cooperative_problem_solver(
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

        elif args.subcommand == "demo":
            wf = args.workflow
            ag = args.agent
            if wf in ("phantoms", "phantom-swarm"):
                res = scenarios.run_phantom_shape_swarm(work_dir=args.work_dir, agent=ag)
                print(json.dumps({
                    "agent_used": res["agent_used"],
                    "spic6": res["spic6"],
                    "ahsn": res["ahsn"],
                    "agent_outputs": res.get("agent_outputs", []),
                }, indent=2, ensure_ascii=False))
            elif wf in ("privacy", "hipaa-pipeline"):
                res = scenarios.run_hipaa_privacy_pipeline(work_dir=args.work_dir, agent=ag)
                print(json.dumps(res["audit"], indent=2, ensure_ascii=False))
            elif wf in ("multimodal", "multimodal-swarm"):
                res = scenarios.run_multimodal_imaging_swarm(work_dir=args.work_dir, agent=ag)
                print(json.dumps({
                    "agent_used": res["agent_used"],
                    "ct": res["ct_bodycomp"],
                    "mri": res["mri_steatosis"],
                    "pet": res["pet_suv"],
                    "agent_outputs": res.get("agent_outputs", []),
                }, indent=2, ensure_ascii=False))
            elif wf in ("lung1", "lung1-audit"):
                res = scenarios.run_lung1_audit_pipeline(work_dir=args.work_dir, agent=ag)
                print(json.dumps(res["review"], indent=2, ensure_ascii=False))
            elif wf in ("opencode", "opencode-free"):
                res = scenarios.run_opencode_free_demo(work_dir=args.work_dir)
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
                res = scenarios.run_all_demos(work_dir=args.work_dir, agent=ag, render_dashboard=True)
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
