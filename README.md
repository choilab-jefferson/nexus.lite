# nexus-lite

| Interactive Colab Notebook | Description |
| :--- | :--- |
| [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/choilab-jefferson/nexus.lite/blob/main/notebooks/nexus_lite_colab_demo.ipynb) | **Core Multi-Agent Demo**: Persistent `dtach` PTYs, message bus pointer injection, swarms, role pipelines, remote bridge, and live `agy` CLI orchestration |
| [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/choilab-jefferson/nexus.lite/blob/main/notebooks/qradiomics_medimage_colab_demo.ipynb) | **QRadiomics + MedImage Multi-Agent Demo**: 3D nodule spiculation/AHSN swarm, HIPAA DICOM de-identification pipeline, multi-modality CT/MRI/PET swarm, and Lung1 data-leakage & reproducibility audit |

Ultra-simple Colab-ready multi-agent primitives for Nexus.

`nexus-lite` turns standalone **CLI coding agents** (`agy`, `opencode`, and `claude`) into a coordinated multi-agent system—providing persistent PTY control (`dtach` + `pyte`), inter-agent messaging with PTY pointer delivery (`[nexus] dm`), native `stm/1` short-term memory (`<nexus_stm>`), parallel swarm & role-pipeline delegation, 5-turn peer-to-peer A2A review loops (`interact`), autonomous 5-phase cooperative problem-solving swarms (`cooperate`), and a local Markdown wiki with full-text search (FTS5). Unlike prompt-chaining API libraries, `nexus-lite` orchestrates real coding agents with full shell and filesystem access while requiring **no external server daemons and zero infrastructure setup**, making it ideal for Google Colab, Jupyter notebooks, and lightweight research workflows.

- **Multi-Agent CLI Harness & Automatic Quota Fallback (`agy` + `opencode` + `claude`)**: Orchestrate heavy reasoning, adversarial `pytest` authoring, and closed-loop repair across **`agy`** (Google Antigravity CLI), **`opencode`** (`opencode/mimo-v2.6-flash-free`, `opencode/mimo-v2.6-flash-free`, `opencode/big-pickle`—which work out-of-the-box with **zero signup and zero API key**), and **`claude`** (Claude Code). If `claude` hits its weekly limit or `opencode` hits its daily free-tier limit, `nexus-lite` automatically falls back in-place (`fallback_cmd`) to available agents.

## Features

1. **Persistent PTY Controller (`nexus_lite.pty`)**:
   - Native `dtach` wire protocol client (`MSG_PUSH`, `MSG_ATTACH`, `MSG_DETACH`, `MSG_WINCH`).
   - Terminal screen capture and VT rendering via `pyte`.
   - Concurrency safety with `flock` on `{sock}.lock`.
   - Multi-agent busy/idle classifier (`claude`, `agy`, `gemini`, `copilot`, `opencode`, `bash`).
   - Keystroke injection (`keys`) and automated wait loops (`wait`).

2. **File & SQLite Message Bus (`nexus_lite.core.bus_*`)**:
   - Zero-broker pub/sub over SQLite WAL and `.nexus/inbox/<msg_id>.md`.
   - Real-time PTY pointer line injection: delivers `[nexus] <kind> <id> <path> - <summary>` directly into active PTY sessions.

3. **Native `stm/1` Short-Term Memory Store (`nexus_lite.stm`)**:
   - File-backed `stm/1` nodes (`.nexus/stm/stm-<project_id>/node/<id>.md`) following `Files = Source of Truth`.
   - Deterministic `<nexus_stm>` JSON/fenced prompt-envelope packing (`nl.stm_pack()`) for cross-turn and cross-agent continuity.

4. **Subtask Delegation, Multi-Agent Swarms & Autonomous Problem Solving (`nexus_lite.core`)**:
   - Executes leaf subagents via subprocess or background PTY sessions with in-place quota/auth fallback (`fallback_cmd`).
   - Enforces recursion guards (`NEXUS_DELEGATION_DEPTH` max 2) and generates persistent artifacts in `.nexus/artifacts/<task_id>/`.
   - **5-Turn Peer-to-Peer Interaction (`nl.run_agent_interaction`)**: Direct `[nexus] dm` exchange across `agy-architect` → `code-driver` → `opencode-reviewer` → `code-driver` → `agy-architect`.
   - **Autonomous 5-Phase Cooperative Problem Solver (`nl.run_cooperative_problem_solver`)**: Takes any arbitrary problem `--goal` and coordinates `thinker-architect` (`agy`), `algo-driver` (`opencode`), `test-engineer` (`agy`), `leaf-scout` (`opencode/big-pickle`), and `peer-verifier` through contract decomposition, parallel code + adversarial `pytest` generation, live `pytest` execution, `<nexus_stm>` closed-loop repair, and consensus sign-off.

5. **Markdown Wiki with FTS5 (`nexus_lite.core.wiki_*`)**:
   - Plain markdown documentation in `.nexus/wiki/<slug>.md`.
   - Full-text search powered by SQLite FTS5 with snippet generation.

6. **Universal Environment Support (`ensure_env`, `detect_env`, `ensure_colab`)**:
   - Universal runtime detection: Google Colab, VS Code Dev Containers, GitHub Codespaces, Docker, RunPod/Vast.ai, or Local Linux/macOS.
   - One-call `ensure_env()` bootstraps `dtach`, initializes `.nexus/`, and launches the bridge dashboard server on port 8765.
   - Ready-to-use `.devcontainer/devcontainer.json` configuration with automatic port forwarding.

7. **Cloudflared Remote Bridge (`nexus_lite.bridge`)**:
   - Auto-configures `cloudflared` to establish an ephemeral `trycloudflare.com` tunnel.
   - Bearer token-authenticated HTTP JSON API server with endpoints for code execution, PTY sessions, bus messages, `stm/1` memory, swarms, pipelines, A2A `interact`, 5-phase `cooperate`, and wiki operations.
   - Zero-dependency `RemoteColab` client allows any local agent (`agy`, `claude`, `opencode`) to execute remote GPU code and orchestrate multi-agent workflows.

8. **Live HTML5 Multi-Agent Dashboard (`nexus_lite.viz`)**:
   - Standalone, zero-CDN HTML5 dashboard with compact 2x2 layout: KPI summary, inline SVG agent topology graph (including curved P2P DM edges, sequence diagram, and 5-phase cooperative timeline), dark PTY terminal screens, task kanban board, and message timeline.
   - Accessible inline in notebooks or globally via the remote bridge `/dashboard?token=<token>&refresh=3`.

---

## Installation & Environments

```bash
pip install git+https://github.com/choilab-jefferson/nexus.lite.git
```

### Universal One-Liner (All Environments)

`nexus-lite` provides a single entrypoint `nl.ensure_env()` that automatically detects your environment and sets up the workspace:

```python
import nexus_lite as nl

# Detects Colab, Codespaces, Dev Containers, Docker, or Local host
info = nl.ensure_env()
print(f"Runtime: {info['env']}, Workspace: {info['workspace']}")
```

### Supported Environments

#### 1. Google Colab
Run in a Colab notebook cell:
```python
!apt-get update -qq && apt-get install -y -qq dtach
!pip install -q git+https://github.com/choilab-jefferson/nexus.lite.git

import nexus_lite as nl
info = nl.ensure_env()  # Auto-detects Colab, defaults mount_drive=True, start_tunnel=True
```

#### 2. VS Code Dev Containers & GitHub Codespaces
A pre-configured dev container is included in `.devcontainer/devcontainer.json` (`python:3.12` base image, `dtach` system package, `pip install -e .[dev]`, and port 8765 auto-forwarding).
- Open the repository in VS Code using **Dev Containers: Reopen in Container** or launch in **GitHub Codespaces**.
- Call `nl.ensure_env()`. Port `8765` is forwarded automatically.
- Open the compact 2x2 live dashboard in your browser:
  `http://127.0.0.1:8765/dashboard?token=<token>&refresh=3`

#### 3. Docker & Local Linux / macOS
```bash
# Ubuntu / Debian
sudo apt-get install -y dtach
pip install git+https://github.com/choilab-jefferson/nexus.lite.git
```
```python
import nexus_lite as nl
info = nl.ensure_env()
# Bridge server runs locally on http://127.0.0.1:8765
```

#### 4. RunPod / Vast.ai Headless GPU Pods
For remote GPU cloud instances behind NAT without open inbound ports, enable Cloudflare tunneling:
```python
import nexus_lite as nl
info = nl.ensure_env(start_tunnel=True)
print("Public 2x2 Dashboard:", info["bridge"]["dashboard_url"] + "&refresh=3")
```

---

## Quickstart (Python API)

```python
import nexus_lite as nl

# 1. Initialize workspace
nl.init()

# 2. Launch an interactive CLI agent in PTY
nl.pty.spawn("bash", name="shell1")
nl.pty.send("shell1", "echo 'Hello from agent'")
screen = nl.pty.read("shell1")
print(screen)

# 3. Send message across agents
nl.bus_send(target="shell1", message="Review task artifact 42", kind="dm")

# 4. Delegate background task
res = nl.delegate(
    agent_cmd="python3 -c 'print(\"Task Done\")'",
    prompt="Perform computation",
    caller="shell1",
    bg=False,
)
print("Task status:", res["status"], "rc:", res["rc"])

# 5. Local Wiki with FTS5 search
nl.wiki_write("architecture", "# Architecture Overview\nNexus-lite uses SQLite WAL.")
results = nl.wiki_search("SQLite")
print("Search results:", results)

# 6. Remote Bridge & Client (Google Colab <-> Local Agent)
# In Colab:
info = nl.start_bridge(port=8765, tunnel=True)
print("Public Dashboard:", info["dashboard_url"])

# From local agent machine:
remote = nl.RemoteColab(info["local_url"], token=info["token"])
gpu_check = remote.exec(python="import torch; print('CUDA:', torch.cuda.is_available())")
print(gpu_check["stdout"])
nl.stop_bridge()
```

---

## CLI Reference

```bash
# Initialize workspace (.nexus/)
nexus-lite init [--workspace .nexus]

# Manage PTY sessions
nexus-lite pty new "bash" --name worker1
nexus-lite pty list
nexus-lite pty send worker1 "ls -la"
nexus-lite pty keys worker1 enter
nexus-lite pty read worker1 --cols 160 --rows 45
nexus-lite pty wait worker1 --provider bash --timeout 10
nexus-lite pty kill worker1

# Message bus
nexus-lite bus send worker1 "Please review file" --sender orchestrator --kind dm
nexus-lite bus read --limit 10

# Task delegation
nexus-lite delegate "python3 worker.py" "Run pipeline" --caller worker1

# Wiki management
nexus-lite wiki write getting-started "# Getting Started Guide"
nexus-lite wiki read getting-started
nexus-lite wiki search "Guide"
nexus-lite wiki list

# Chat helper
nexus-lite chat worker1 "What is current status?" --provider bash

# Remote Bridge Server & Client
nexus-lite bridge start --port 8765
nexus-lite bridge status
nexus-lite bridge exec "nvidia-smi" --url "https://xyz.trycloudflare.com" --token "secret"
nexus-lite bridge stop

# HTML5 Dashboard
nexus-lite dashboard --out dashboard.html

# Short-Term Memory (stm/1)
nexus-lite stm write --kind decision --status done --summary "Use modified Z-score"
nexus-lite stm list
nexus-lite stm pack

# 5-Turn Peer-to-Peer Agent Interaction (interact) & Autonomous 5-Phase Cooperative Swarm (cooperate)
nexus-lite interact --goal "Implement and peer-review a 3D NIfTI affine validator"
nexus-lite cooperate \
  --goal "Design, implement, and adversarially test a 3D Radiomics Z-Score Harmonizer and IQR Outlier Quarantine module (scanner_harmonizer.py)" \
  --module-name scanner_harmonizer \
  --architect-agent agy \
  --verifier-agent opencode/mimo-v2.6-flash-free

# Practical QRadiomics + MedImage Multi-Agent Demos (with agy, multi-tier, or claude)
nexus-lite demo all --agent agy           # Run all 4 medical imaging & radiomics demos via Antigravity (agy)
nexus-lite demo all --agent multi-tier    # Run all 4 demos across OpenCode + AGY
nexus-lite demo all --agent claude        # Run all 4 demos via Claude Code (claude, auto-falls back if quota reached)
nexus-lite demo phantom-swarm --agent agy # Demo 1: 3D lung nodule spiculation (spic6) & 180-D AHSN swarm
nexus-lite demo hipaa-pipeline --agent claude # Demo 2: HIPAA Safe Harbor DICOM de-identification role pipeline
nexus-lite demo multimodal-swarm --agent agy  # Demo 3: CT body composition, CHAOS MRI steatosis & PET SUV swarm
nexus-lite demo lung1-audit --agent claude    # Demo 4: NSCLC-Radiomics (Lung1) data-leakage & reproducibility audit
```

---

## Practical Medical Imaging & Radiomics Demos (`qradiomics` + `medimage`)

`nexus-lite` ships with built-in multi-agent workflows (`nexus_lite.demos`) bridging [`qradiomics`](https://github.com/choilab-jefferson/qradiomics) and the 13-chapter [`medimage`](https://github.com/choilab-jefferson/medimage) Google Colab curriculum, orchestrating live `agy` or `claude` agents (`agent="agy" | "claude" | "auto" | "python"`):

```python
import nexus_lite as nl

# 1. 3D Pulmonary Nodule Morphology Swarm (Choi 2014 CMPB AHSN + Choi 2021 CMPB spic6)
d1 = nl.run_phantom_shape_swarm(agent="agy")

# 2. HIPAA Safe Harbor DICOM De-Identification Pipeline (driver -> verifier -> refiner, medimage Ch.5)
d2 = nl.run_hipaa_privacy_pipeline(agent="claude")

# 3. Multi-Modality Quantitative Imaging Swarm: CT L3 Body Comp, MRI Steatosis, PET SUV (medimage Ch.6-8)
d3 = nl.run_multimodal_imaging_swarm(agent="agy")

# 4. NSCLC-Radiomics (Lung1) Target-Leakage & Reproducibility Audit Pipeline (medimage Ch.12-13)
d4 = nl.run_lung1_audit_pipeline()

# Render live 2x2 HTML5 multi-agent dashboard
nl.dashboard()
```

---

## Using loom

`nexus_lite.loom` wraps the `loom` agent harness as a subprocess (stdlib only). Set `LOOM_BIN` or put `loom` on `PATH`.

```python
from nexus_lite.loom import probe, LoomAdapter
print(probe().available)            # never raises; False if loom is missing
r = LoomAdapter().run_prompt("Say hello", provider="mock")
print(r.status, r.ok)               # "unavailable" when loom is not installed
```

Exit codes map to `ok`, `task_failure` (retryable), `harness_or_policy_error`, `escalation_required`, `budget`, `interrupted`, else `unknown`; `loom capabilities --json` overrides the built-in table. Install path is TBD (`LOOM_INSTALL_URL`).

## Acknowledgements

We gratefully acknowledge **Google** for providing **Gemini 4 Argon** access and support, which powered the multi-agent orchestration, quantitative radiomics synthesis, and automated methodological auditing across `nexus-lite`, [`qradiomics`](https://github.com/choilab-jefferson/qradiomics), and [`medimage`](https://github.com/choilab-jefferson/medimage).

*Computational Healthcare & Oncology Informatics (CHOI) Lab, Department of Radiation Oncology, Sidney Kimmel Medical College at Thomas Jefferson University* · MIT License

