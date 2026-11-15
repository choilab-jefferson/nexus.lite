---
name: nexus-colab
description: Two-tier Google Colab agent skill covering Tier 1 in-notebook bootstrap (colab.ensure_env(), local /content/.nexus kernel + Drive ~/.gemini & artifact sync, and nl.delegate / nl.run_swarm / nl.run_pipeline with zero tunnel) and Tier 2 optional remote cloudflared tunnel (ColabTunnelClient / nexus-lite-colab) for GPU execution, persistent PTYs, and live dashboards.
---

# Nexus-Colab Two-Tier Google Colab Skill

The `nexus-colab` skill supports two deployment tiers for running Antigravity (`agy`), Claude Code, or Nexus agents on Google Colab, remote GPU servers, or containerized runtimes with **zero infrastructure setup, zero port forwarding, and zero open firewall ports**:

- **Tier 1 (In-Notebook Bootstrap — Default, Zero Tunnel)**: Initialize inside a Colab notebook with `colab.ensure_env()` and orchestrate CLI/Python workloads locally via `nl.delegate`, `nl.run_swarm`, and `nl.run_pipeline`.
- **Tier 2 (Remote Cloudflared Tunnel — Optional)**: Expose the Colab runtime via `tunnel.start_tunnel(tunnel=True)` so external local agents (`ColabTunnelClient` or `nexus-lite-colab`) can run GPU jobs, control persistent PTYs, and inspect live HTML5 dashboards remotely.

### Google Colab Execution Environment Highlights
- **Pre-installed `agy` CLI**: Google Colab environments pre-install Antigravity CLI (`agy`) since 2026-09-16 alongside official `google-colab-cli` and `colab-mcp`, providing ready-to-use multi-agent CLI capability on PATH without manual setup.
- **Local VM Runtime + Drive Sync & Non-Destructive Credentials**: `colab.ensure_env()` / `colab.ensure_colab()` keeps active SQLite WAL state, the message bus, and `AF_UNIX` PTY sockets on **local VM disk** (`/content/.nexus` and `/tmp/nexus-lite-<tag>`, since Google Drive FUSE does not support `AF_UNIX` `bind()` or reliable SQLite file locks), non-destructively symlinks `/root/.gemini` to `/content/drive/MyDrive/.gemini` (or `/content/drive/MyDrive/nexus-lite/.gemini`), and uses explicit `colab.colab_sync_to_drive()` / `colab.colab_sync_from_drive()` to sync `artifacts/`, `wiki/`, and `stm/` with `/content/drive/MyDrive/nexus-lite`.
- **Persistent Cloudflared Tunnel & Standalone Live Dashboard (Tier 2)**: `tunnel.start_tunnel(tunnel=True)` exposes an ephemeral `trycloudflare.com` tunnel and auto-detects Colab proxy ports. Standalone dashboards support auto-refresh via `&refresh=3`.

---

## 1A. Tier 1 — In-Notebook Agent Bootstrap (Zero Tunnel, Recommended Default)

Use Tier 1 inside Google Colab notebooks (including the `"Drive this with an agent"` footer in `qradiomics` notebooks) when no external tunnel is required:

```python
!pip install -qU nexus-lite "qradiomics>=0.9.8" "numpy>=2.0"

import nexus_lite as nl
from nexus_lite import colab

# Initializes /content/.nexus locally and symlinks /root/.gemini to Drive if mounted
env = colab.ensure_env()
print("Workspace:", env["workspace"], "| Runtime:", env["env"])

# Delegate or pipeline CLI steps (e.g. pure-Python rtools extraction, zero pyradiomics needed)
res = nl.delegate(
    task_id="qradiomics-check",
    cmd="qr --version && qr pattern list",
    prompt="Verify qradiomics CLI and available patterns",
    caller="colab-notebook",
)
print(res["output"])

# Optional: export artifacts/, wiki/, and stm/ to /content/drive/MyDrive/nexus-lite
# colab.colab_sync_to_drive()
```

---

## 1B. Tier 2 — Optional Remote Cloudflared Tunnel

When connecting a local workstation agent to a Colab runtime over `ColabTunnelClient` or `nexus-lite-colab`:

1. The remote Colab runtime starts `nexus_lite.colab.tunnel.start_tunnel()`, running an HTTP JSON Tunnel Server over an ephemeral `trycloudflare.com` tunnel via `cloudflared`.
2. The local agent uses `nexus_lite.colab.tunnel.ColabTunnelClient(url, token)` or the `nexus-lite-colab` CLI to execute GPU workloads, spawn and drive persistent PTY sessions, orchestrate multi-agent swarms and pipelines, and inspect live HTML5 dashboards.

Run in a Google Colab notebook cell:

```python
!pip install -qU nexus-lite

from google.colab import drive
drive.mount("/content/drive")

import secrets
import nexus_lite as nl
from nexus_lite import colab
from nexus_lite import demos
from nexus_lite.colab import tunnel

# 1. Initialize Colab environment:
# Uses /content/.nexus locally for SQLite/PTY sockets and non-destructively symlinks ~/.gemini to Drive.
env = colab.ensure_colab()
print("Workspace initialized at:", env["workspace"])

# 2. Inspect tunnel connection info
token = secrets.token_hex(16)
tunnel_info = tunnel.start_tunnel(port=8765, tunnel=True, token=token)

print("Remote Tunnel Public URL:", tunnel_info["public_url"])
print("Live HTML5 Dashboard (Cloudflare):", tunnel_info["dashboard_url"])
if tunnel_info.get("colab_dashboard_url"):
    print("Live HTML5 Dashboard (Colab Proxy):", tunnel_info["colab_dashboard_url"])
print("Auth Token:", tunnel_info["token"])
```

Output:
```text
Workspace initialized at: /content/.nexus
Remote Tunnel Public URL: https://random-subdomain.trycloudflare.com
Live HTML5 Dashboard (Cloudflare): https://random-subdomain.trycloudflare.com/dashboard?token=a8f...
Live HTML5 Dashboard (Colab Proxy): https://...-colab.googleusercontent.com/dashboard?token=a8f...
Auth Token: a8f73b8902c384...
```

> [!WARNING]
> **Do NOT call `tunnel.stop_tunnel()` automatically at the end of runs or notebooks.**
> The tunnel server and cloudflared tunnel run in persistent PTY background sessions to maintain connectivity for external agents, monitoring tools, and live dashboards. Calling `stop_tunnel()` immediately terminates the tunnel, resulting in 502 Bad Gateway / Cloudflare Error 1033. To stop manually, invoke `tunnel.stop_tunnel()`.

---

## 2. Connecting from Local Agents (Python Client)

Any local agent or script connects using `nexus_lite.colab.tunnel.ColabTunnelClient`:

```python
from nexus_lite.colab.tunnel import ColabTunnelClient

# Connect to Colab runtime
remote = ColabTunnelClient(
    url="https://random-subdomain.trycloudflare.com",
    token="a8f73b8902c384...",
)

# 1. Verify health and environment
health = remote.health()
print("Remote health:", health)

# 2. Execute GPU / PyTorch code on Colab
res = remote.exec(python="""
import torch
print("CUDA Available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("Device Name:", torch.cuda.get_device_name(0))
""")
print(res["stdout"])

# 3. Execute remote shell commands
nvidia_smi = remote.exec(cmd="nvidia-smi")
print(nvidia_smi["stdout"])
```

---

## 3. Remote Persistent PTY Control

Control interactive sessions (e.g. bash, Python REPL, or remote CLI agents) across network reconnects:

```python
# Spawn persistent session on Colab
sess = remote.pty_spawn("bash --norc", name="gpu-train")

# Send training command
remote.pty_send(sess, "python3 train.py --epochs 10", enter=True)

# Read terminal output
screen = remote.pty_read(sess, cols=160, rows=45)
print(screen)

# Wait for completion or idle state
screen = remote.pty_wait(sess, provider="bash", timeout=60.0)

# List and clean up sessions
sessions = remote.pty_list()
remote.pty_kill(sess)
```

---

## 4. Multi-Agent Swarms & Sequential Pipelines

Orchestrate complex distributed workloads on the remote Colab GPU:

### Parallel Swarm (`run_swarm`)
```python
specs = [
    {
        "task_id": "download-weights",
        "cmd": "python3 download_model.py",
        "prompt": "Download model weights",
    },
    {
        "task_id": "prepare-data",
        "cmd": "python3 prepare_dataset.py",
        "prompt": "Preprocess dataset",
    },
]

results = remote.run_swarm(specs, caller="local-orchestrator", timeout=120.0)
for r in results:
    print(f"[{r['status']}] {r['task_id']}: {r['output']}")
```

### Sequential Pipeline (`run_pipeline`)
```python
stages = [
    {
        "name": "trainer",
        "cmd": "python3 -c 'print(\"Model loss: 0.014\")'",
        "prompt": "Train model on Colab GPU",
    },
    {
        "name": "evaluator",
        "cmd": "python3 -c 'print(\"Benchmark AUC: 0.985\")'",
        "prompt": "Evaluate checkpoint: {prev_output}",
    },
]

pipeline_res = remote.run_pipeline(
    stages,
    initial_prompt="Train and evaluate checkpoint",
    caller="local-orchestrator",
)
print("Pipeline Output:", pipeline_res["final_output"])
```

### 5-Turn Peer-to-Peer Agent Interaction (`run_agent_interaction_loop`) & Autonomous 5-Phase Cooperative Swarm (`run_cooperative_problem_solver`)
```python
# 5-Turn P2P DM interaction loop across thinker-architect -> code-driver -> peer-verifier
a2a_res = remote.run_agent_interaction_loop(
    goal="Verify medical imaging coordinate transform and HU windowing",
)

# Autonomous 5-phase multi-agent problem-solving swarm (agy + opencode)
coop_res = remote.run_cooperative_problem_solver(
    goal="Design, implement, and adversarially test a 3D Radiomics Z-Score Harmonizer and IQR Outlier Quarantine module (scanner_harmonizer.py)",
    module_name="scanner_harmonizer",
)
print("Final Pytest Exit Code:", coop_res["final_pytest_rc"], "Verdict:", coop_res["consensus_verdict"])
```

---

## 5. Remote Message Bus, Short-Term Memory (`stm/1`) & Wiki

### Native `stm/1` Short-Term Memory (`<nexus_stm>`)
STM = live working state. (MTM = flow carried across interruptions, transported by the bus below; LTM = the wiki, the authoritative durable record; MTM+ is retired.) `agent=` below is a free-form role label, not a registered identity.
```python
# Record an stm/1 decision node on the remote workspace
node = remote.stm_write(
    kind="decision",
    status="done",
    summary="Use modified Z-score for scanner harmonization",
    agent="thinker-architect",
)

# Pack deterministic <nexus_stm> envelope
envelope = remote.stm_pack(as_json=True)
print("Packed STM nodes:", len(envelope["nodes"]))
```

### Agent Bus (Inter-agent communication and PTY pointer line injection)
`bus_send` is the lite verb for nexus say/dm/relay; target `ALL` broadcasts to live sessions. A "session" is a persistent PTY, never a delegated worker; completion of a delegated worker arrives as `TASK_COMPLETE` / `TASK_ESCALATE` messages to its caller.
```python
# Send message to an active PTY session on Colab
remote.bus_send(
    target="gpu-train",
    message="Interrupt training if validation loss plateaus",
    sender="supervisor",
    kind="dm",
)

# Read message history
messages = remote.bus_read(target="gpu-train", limit=10)
```

### Remote FTS5 Markdown Wiki
```python
# Document results on Colab workspace
remote.wiki_write("experiments/run-01", "# Run 01\nModel AUC: 0.985 on Colab T4 GPU.")

# Search wiki documents
hits = remote.wiki_search("AUC 0.985")
print("Found articles:", hits)
```

---

## 6. Live HTML5 Multi-Agent Dashboard

View the real-time visualization dashboard from anywhere:
1. **In Browser**: Navigate directly to `https://<public_url>/dashboard?token=<token>&refresh=3` (or via Colab proxy URL). Passing `&refresh=3` auto-refreshes the page every 3 seconds via injected `<meta http-equiv='refresh' content='3'>`.
2. **From Python**:
   ```python
   html = remote.dashboard_html()
   # Display in Jupyter or save to file
   with open("remote_colab_dashboard.html", "w") as f:
       f.write(html)
   ```

---

## 7. CLI Usage

You can also interact with the remote tunnel from the terminal:

```bash
# Execute remote command
nexus-lite-colab exec "nvidia-smi" \
  --url "https://random-subdomain.trycloudflare.com" \
  --token "a8f73b8902c384..."

# Inspect tunnel status on host
nexus-lite-colab status

# Run built-in QRadiomics + MedImage multi-agent workflows
nexus-lite-demo demo all
```

---

## 8. Built-in QRadiomics + MedImage Multi-Agent Workflows (`nexus_lite.demos`)

`nexus-lite` includes domain-grounded medical imaging & radiomics multi-agent workflows powered by `choilab-jefferson/qradiomics` (`v0.9.8+`, using the pure-Python IBSI-1 `rtools` engine straight from PyPI with `pyradiomics` strictly optional; `v0.9.9+` makes `rtools` the default engine everywhere) and `choilab-jefferson/medimage`. See the companion Colab notebook `notebooks/qradiomics_medimage_colab_demo.ipynb` (including Section 9 / Demo 8 end-to-end `qr select validate` -> `qr extract --engine rtools` -> `qr results merge` -> `qr analyze survival` + FTS5 wiki indexing):

1. **`demos.run_phantom_shape_swarm()`**: 3-worker parallel swarm computing Choi 2021 *CMPB* `spic6` (`Np`, `Na`, `Nl`, `Na_att`, `s1`, `s2`) and Choi 2014 *CMPB* 180-D `AHSN` + `wall_eliminate` across 5 3D lung nodule phantoms.
2. **`demos.run_hipaa_privacy_pipeline()`**: 3-stage role pipeline (`driver` -> `verifier` -> `refiner`) planting synthetic PHI in DICOM slices, running `qr anonymize --replace-pid --pid-salt`, auditing `0` residual PHI findings, and verifying 100% HU pixel/geometry parity (`medimage` Ch.5).
3. **`demos.run_multimodal_imaging_swarm()`**: 3-worker parallel swarm across CT L3 body composition (`medimage` Ch.6), CHAOS T1-DUAL MRI Dixon liver steatosis vs. spleen noise floor (`medimage` Ch.7), and ACRIN 6668 FDG-PET/CT QIBA SUV calibration & longitudinal ΔSUV (`medimage` Ch.8).
4. **`demos.run_lung1_audit_pipeline()`**: 3-stage role pipeline (`driver` -> `navigator` -> `verifier`) reproducing the `OS_months`/`OS_event` target-leakage bug (`AUC = 1.000` vs. leak-free CV) and auditing multiple comparisons (`Bonferroni`) and sample-size scaling on NSCLC-Radiomics Lung1 (`medimage` Ch.12 & Ch.13).
