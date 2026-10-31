---
name: nexus-colab
description: Connect local agents (Agy, Claude, Nexus) to a remote Google Colab or headless GPU runtime via cloudflared bridge and execute GPU workloads, control dtach PTYs, run multi-agent swarms and pipelines, and monitor live dashboards.
---

# Nexus-Colab Remote Bridge Skill

The `nexus-colab` skill enables local agents (Agy, Claude Code, Nexus) or developers to connect securely to a remote Google Colab instance, remote GPU server, or containerized runtime with **zero infrastructure setup, zero port forwarding, and zero open firewall ports**.

### Google Colab Execution Environment Highlights
- **Pre-installed `agy` CLI**: Google Colab environments pre-install Antigravity CLI (`agy`) since 2026-09-16 alongside official `google-colab-cli` and `colab-mcp`, providing ready-to-use multi-agent CLI capability on PATH without manual setup.
- **Default Google Drive Mount & Non-Destructive Credentials**: `nl.ensure_colab()` automatically defaults persistent storage to `/content/drive/MyDrive/nexus-lite` when running in Colab and safely links `/root/.gemini` credentials to Drive without overwriting existing files.
- **Persistent Cloudflared Tunnel & Standalone Live Dashboard**: `nl.start_bridge(tunnel=True)` exposes an ephemeral `trycloudflare.com` tunnel and auto-detects Colab proxy ports. Standalone dashboards support auto-refresh via `&refresh=3`.

Under the hood:
1. The remote Colab runtime starts `nexus_lite.start_bridge()`, running an HTTP JSON Bridge Server over an ephemeral `trycloudflare.com` tunnel via `cloudflared`.
2. The local agent uses `nexus_lite.RemoteColab(url, token)` or the `nexus-lite bridge` CLI to execute GPU workloads, spawn and drive persistent `dtach` PTY sessions, orchestrate multi-agent swarms and pipelines, and inspect live HTML5 dashboards.

---

## 1. Initializing Environment and Starting Bridge in Google Colab

Run in a Google Colab notebook cell:

```python
!apt-get update -qq && apt-get install -y -qq dtach
!pip install -q git+https://github.com/choilab-jefferson/nexus.lite.git

from google.colab import drive
drive.mount("/content/drive")

import nexus_lite as nl

# 1. Initialize Colab environment:
# Automatically mounts /content/drive/MyDrive/nexus-lite, non-destructively syncs ~/.gemini credentials,
# and starts the bridge server and cloudflared tunnel if not already running.
env = nl.ensure_colab()
print("Workspace initialized at:", env["workspace"])

# 2. Inspect bridge connection info
bridge_info = env.get("bridge") or nl.start_bridge(port=8765, tunnel=True)

print("Remote Bridge Public URL:", bridge_info["public_url"])
print("Live HTML5 Dashboard (Cloudflare):", bridge_info["dashboard_url"])
if bridge_info.get("colab_dashboard_url"):
    print("Live HTML5 Dashboard (Colab Proxy):", bridge_info["colab_dashboard_url"])
print("Auth Token:", bridge_info["token"])
```

Output:
```text
Workspace initialized at: /content/drive/MyDrive/nexus-lite
Remote Bridge Public URL: https://random-subdomain.trycloudflare.com
Live HTML5 Dashboard (Cloudflare): https://random-subdomain.trycloudflare.com/dashboard?token=a8f...
Live HTML5 Dashboard (Colab Proxy): https://...-colab.googleusercontent.com/dashboard?token=a8f...
Auth Token: a8f73b8902c384...
```

> [!WARNING]
> **Do NOT call `nl.stop_bridge()` automatically at the end of runs or notebooks.**
> The bridge server and cloudflared tunnel run in persistent `dtach` background sessions to maintain connectivity for external agents, monitoring tools, and live dashboards. Calling `stop_bridge()` immediately terminates the tunnel, resulting in 502 Bad Gateway / Cloudflare Error 1033. To stop manually, invoke `nl.stop_bridge()`.

---

## 2. Connecting from Local Agents (Python Client)

Any local agent or script connects using `nexus_lite.RemoteColab`:

```python
from nexus_lite import RemoteColab

# Connect to Colab runtime
remote = RemoteColab(
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

## 3. Remote Persistent PTY Control (`dtach`)

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

You can also interact with the remote bridge from the terminal:

```bash
# Execute remote command
nexus-lite bridge exec "nvidia-smi" \
  --url "https://random-subdomain.trycloudflare.com" \
  --token "a8f73b8902c384..."

# Inspect bridge status on host
nexus-lite bridge status

# Run built-in QRadiomics + MedImage multi-agent workflows
nexus-lite demo all
```

---

## 8. Built-in QRadiomics + MedImage Multi-Agent Workflows (`nexus_lite.demos`)

`nexus-lite` includes four domain-grounded medical imaging & radiomics multi-agent workflows powered by `choilab-jefferson/qradiomics` (`v0.9.5`) and `choilab-jefferson/medimage`:

1. **`nl.run_phantom_shape_swarm()`**: 3-worker parallel swarm computing Choi 2021 *CMPB* `spic6` (`Np`, `Na`, `Nl`, `Na_att`, `s1`, `s2`) and Choi 2014 *CMPB* 180-D `AHSN` + `wall_eliminate` across 5 3D lung nodule phantoms.
2. **`nl.run_hipaa_privacy_pipeline()`**: 3-stage role pipeline (`driver` -> `verifier` -> `refiner`) planting synthetic PHI in DICOM slices, running `qr anonymize --replace-pid --pid-salt`, auditing `0` residual PHI findings, and verifying 100% HU pixel/geometry parity (`medimage` Ch.5).
3. **`nl.run_multimodal_imaging_swarm()`**: 3-worker parallel swarm across CT L3 body composition (`medimage` Ch.6), CHAOS T1-DUAL MRI Dixon liver steatosis vs. spleen noise floor (`medimage` Ch.7), and ACRIN 6668 FDG-PET/CT QIBA SUV calibration & longitudinal $\Delta\text{SUV}$ (`medimage` Ch.8).
4. **`nl.run_lung1_audit_pipeline()`**: 3-stage role pipeline (`driver` -> `navigator` -> `verifier`) reproducing the `OS_months`/`OS_event` target-leakage bug (`AUC = 1.000` vs. leak-free CV) and auditing multiple comparisons (`Bonferroni`) and sample-size scaling on NSCLC-Radiomics Lung1 (`medimage` Ch.12 & Ch.13).
