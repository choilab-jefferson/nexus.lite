#!/usr/bin/env python3
"""Zero-preinstall remote compute + loom IBSI radiomics multi-agent verification.

Runs on:
  - `local`: isolated zero-install sandbox (`python -I` with packed `nexus_lite` kernel)
  - `colab`: ephemeral or active Google Colab HTTP tunnel (`nexus_lite.colab.tunnel`)
             or standalone zero-install Colab bundle (`--bootstrap-out`)
  - `hpc`:   SLURM cluster node (`srun` / `sbatch` / `ssh`)

Each run executes the `nexus_lite.loom_multi_agent` suite across all three IBSI
radiomics tasks (`radiomics-firstorder` [33 hidden tests], `radiomics-texture`
[34 hidden tests], `radiomics-stats` [26 hidden tests] = 93 hidden acceptance tests)
plus the `loom` binary contract probe (`probe()`, `LoomAdapter`, `loom_env()`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nexus_lite as nl
from nexus_lite.colab import tunnel as colab_tunnel


LOOM_RADIOMICS_WORKLOAD_PYTHON = """
import json
import shutil
from nexus_lite import loom
from nexus_lite.loom_multi_agent import available_tasks, build

# 1. Verify loom adapter & environment contract
env_sample = loom.loom_env()
pr = loom.probe()
adapter_info = {
    "min_version": loom.MIN_LOOM_VERSION,
    "available": pr.available,
    "has_loom_binary": loom.find_loom() is not None,
    "env_has_path": "PATH" in env_sample,
}

# 2. Run all 3 IBSI radiomics multi-agent tasks (93 hidden acceptance tests total)
tasks = available_tasks()
task_names = ("radiomics-firstorder", "radiomics-texture", "radiomics-stats")
task_summaries = {}
total_passed = 0
total_tests = 0

for tname in task_names:
    tspec = tasks[tname]
    res = build(
        providers=["mock"],
        loom="",
        extra_env={},
        max_rounds=3,
        baseline=True,
        out=lambda *_a, **_k: None,
        retries=1,
        backoff=0.0,
        task=tspec,
    )
    shutil.rmtree(res["sandbox"], ignore_errors=True)
    shutil.rmtree(res["hidden_dir"], ignore_errors=True)
    if res.get("baseline") and res["baseline"].get("sandbox"):
        shutil.rmtree(res["baseline"]["sandbox"], ignore_errors=True)

    passed = int(res["hidden"]["passed"])
    total = int(res["hidden"]["total"])
    base_passed = int(res["baseline"]["hidden"]["passed"]) if res.get("baseline") else 0
    total_passed += passed
    total_tests += total
    task_summaries[tname] = {
        "verdict": "PASS" if res["ok"] else "FAIL",
        "rounds_used": int(res["rounds"]),
        "multi_agent_passed": passed,
        "multi_agent_total": total,
        "single_agent_baseline_passed": base_passed,
        "modules": list(tspec.FILES),
    }
    nl.stm_write(
        f"loom-{tname}",
        kind="task",
        status="completed" if res["ok"] else "failed",
        summary=f"{tname}: {passed}/{total} hidden tests (baseline {base_passed}/{total})",
    )

nl.wiki_write(
    "loom/radiomics_verification",
    f"# Loom IBSI Radiomics Multi-Agent Verification\\n"
    f"- Total hidden tests passed: {total_passed}/{total_tests}\\n"
    f"- Tasks: {', '.join(task_names)}\\n",
)

report = {
    "loom_adapter": adapter_info,
    "tasks": task_summaries,
    "total_hidden_passed": total_passed,
    "total_hidden_tests": total_tests,
    "all_passed": total_passed == total_tests and total_tests == 93,
    "stm_count": len(nl.stm_list()),
    "wiki_count": len(nl.wiki_list()),
}
(JOB_DIR / "loom_radiomics_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
RESULT = report
"""


def run_loom_radiomics_on_compute(
    target: str = "local",
    *,
    url: str | None = None,
    token: str | None = None,
    bootstrap_out: str | None = None,
    ephemeral_tunnel: bool = False,
) -> dict[str, Any]:
    if target == "colab" and ephemeral_tunnel and not url:
        prev_home = os.environ.get("NEXUS_HOME")
        with tempfile.TemporaryDirectory(prefix="nexus_loom_colab_") as tmp_home:
            os.environ["NEXUS_HOME"] = tmp_home
            info = colab_tunnel.start_tunnel(port=0, tunnel=False)
            try:
                tok = colab_tunnel.token_file_path(info["port"]).read_text(encoding="utf-8").strip()
                return nl.run_on_colab(
                    python=LOOM_RADIOMICS_WORKLOAD_PYTHON,
                    collect_files=["loom_radiomics_report.json"],
                    url=info["local_url"],
                    token=tok,
                )
            finally:
                colab_tunnel.stop_tunnel()
                if prev_home is None:
                    os.environ.pop("NEXUS_HOME", None)
                else:
                    os.environ["NEXUS_HOME"] = prev_home

    kwargs: dict[str, Any] = {}
    if target == "colab":
        kwargs.update({"url": url, "token": token, "bootstrap_out": bootstrap_out})
    return nl.compute_run(
        target,
        python=LOOM_RADIOMICS_WORKLOAD_PYTHON,
        collect_files=["loom_radiomics_report.json"],
        **kwargs,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", choices=("local", "colab", "hpc", "turing"), default="local")
    ap.add_argument("--url", default=None, help="Colab tunnel URL")
    ap.add_argument("--token", default=None, help="Colab tunnel token")
    ap.add_argument("--bootstrap-out", default=None, help="Write standalone zero-install Colab .py bundle")
    ap.add_argument("--ephemeral-tunnel", action="store_true", help="Run over an ephemeral loopback Colab HTTP tunnel")
    args = ap.parse_args(argv)

    res = run_loom_radiomics_on_compute(
        target=args.target,
        url=args.url,
        token=args.token,
        bootstrap_out=args.bootstrap_out,
        ephemeral_tunnel=args.ephemeral_tunnel,
    )
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
