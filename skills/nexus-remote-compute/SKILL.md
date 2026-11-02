---
name: nexus-remote-compute
description: "[Public / Open-Core] Zero-preinstall remote compute dispatch with nexus-lite across any SSH/SLURM HPC cluster (srun, sbatch, ssh), external Google Colab runtime (HTTP tunnel, SSH, or standalone zero-install bootstrap script), and isolated local sandboxes by shipping the stdlib-only nexus_lite kernel with the workload."
---

# Nexus-Lite Zero-Preinstall Remote Compute Skill (`hpc` / `colab` / `local`)

`nexus_lite.compute` enables any local agent or script to execute Python workloads, shell commands, or `nexus-lite` multi-agent CLI workflows on **bare remote environments where `nexus-lite` has never been installed**.

Because `nexus-lite` is built 100% on the Python standard library (~140 KB compressed), `nexus_lite.compute` packs the local `nexus_lite` package into an in-memory ZIP archive, embeds it inside a self-extracting Python script, transfers it with the workload, extracts it to `~/.cache/nexus_lite/runtime/<bundle_sha>` (or `/content/.nexus_runtime` on Colab), initializes an isolated `.nexus` workspace, and returns a structured JSON result.

---

## 1. Environment Variables & SSH Configuration

| Environment Variable | Default | Description |
| :--- | :--- | :--- |
| `NEXUS_HPC_HOST` / `NEXUS_TURING_HOST` | `turing` | SSH host or SSH config alias for the target SLURM HPC login node |
| `NEXUS_HPC_JUMP_HOST` / `NEXUS_TURING_JUMP_HOST` | *(none)* | Optional SSH bastion/jump host used automatically if direct SSH returns exit code 255 |
| `NEXUS_COLAB_URL` | *(auto-discovered from `.nexus/tunnel.json`)* | Active Google Colab tunnel URL (`https://....trycloudflare.com`) |
| `NEXUS_COLAB_TOKEN` | *(auto-discovered from local token file)* | Bearer token for the active Google Colab tunnel |
| `NEXUS_COLAB_SSH` | *(none)* | Optional SSH host alias for a Google Colab SSH tunnel |

---

## 2. Internal / Cluster Compute: SLURM HPC (`hpc` or `turing`)

### CLI Usage (`nexus-lite compute hpc`)

```bash
# 1) Interactive SLURM GPU/CPU compute node execution (mode=srun, default)
#    Inside --python, `nexus_lite` (and alias `nl`), `WORKSPACE`, `JOB_DIR`, and `RESULT` are pre-bound.
nexus-lite compute hpc \
  --host my-cluster-login \
  --mode srun \
  --partition batch \
  --gres gpu:1 \
  --cpus 4 \
  --time 00:10:00 \
  --python "
import subprocess
gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'], text=True).strip()
nl.stm_write('gpu-probe', kind='task', status='completed', summary=gpu)
RESULT = {'gpu': gpu, 'stm_nodes': len(nl.stm_list())}
"

# 2) Stage local script + extra input files to the cluster node and collect output artifacts
nexus-lite compute hpc \
  --host my-cluster-login \
  --mode srun \
  --partition batch \
  --script evaluate.py \
  --file config.json=./local_config.json \
  --collect metrics.json

# 3) Run any nexus-lite CLI subcommand remotely (without nexus-lite installed on the cluster)
nexus-lite compute hpc \
  --host my-cluster-login \
  --mode srun \
  --partition batch \
  --lite "tasks"

# 4) Asynchronous SLURM batch submission (mode=sbatch) and status polling
nexus-lite compute hpc \
  --host my-cluster-login \
  --mode sbatch \
  --partition batch \
  --gres gpu:1 \
  --cpus 8 \
  --time 02:00:00 \
  --script train_job.py

# Poll batch job status and retrieve result.json / slurm.out:
nexus-lite compute hpc --host my-cluster-login --status <job_id> --slurm-id <slurm_job_id>

# 5) Direct SSH login-node execution (mode=ssh, no SLURM queue)
nexus-lite compute hpc --host my-cluster-login --mode ssh --python "RESULT = nl.__version__"
```

### Python API (`nexus_lite.compute_run` / `nexus_lite.run_on_turing`)
```python
import nexus_lite as nl
from nexus_lite import demos

res = nl.compute_run(
    "hpc",
    ssh_host="my-cluster-login",
    mode="srun",
    partition="batch",
    gres="gpu:1",
    cpus=2,
    time_limit="00:05:00",
    python="""
nl.wiki_write("experiments/run1", "# Run 1 Complete")
RESULT = {"wiki_count": len(nl.wiki_list())}
""",
)
print(res["hostname"], res["slurm_job_id"], res["result"])
```

---

## 3. External Compute: Google Colab (`colab`)

`nexus-lite compute colab` ships the self-extracting `nexus_lite` bundle to an external Google Colab runtime, even when `nexus-lite` has **not** been installed in that Colab session.

### Mode A: Active Tunnel (`--url` or `NEXUS_COLAB_URL` / `.nexus/tunnel.json`)
```bash
nexus-lite compute colab \
  --url "$NEXUS_COLAB_URL" \
  --token "$NEXUS_COLAB_TOKEN" \
  --python "
import torch
nl.stm_write('colab-gpu', kind='task', status='completed', summary=str(torch.cuda.is_available()))
RESULT = {'cuda': torch.cuda.is_available(), 'lite_version': nl.__version__}
"
```

### Mode B: Zero-Install Standalone Bootstrap Script (`--bootstrap-out`)
For a completely bare Google Colab notebook (zero `git clone`, zero `pip install`), generate a single self-contained `.py` file embedding the entire `nexus_lite` kernel + your task (and optionally `--start-tunnel`):

```bash
nexus-lite compute colab \
  --bootstrap-out ./colab_job_bundle.py \
  --start-tunnel \
  --python "RESULT = demos.run_phantom_shape_swarm(agent='python')['spic6']"
```

Then run in any bare Google Colab cell:
```python
!python3 /content/drive/MyDrive/colab_job_bundle.py
```

### Mode C: Headless Colab GPU/CPU Provisioning (`google-colab-cli` / `colab`)
When authenticated via `colab auth login` (one-time browser OAuth), provision a Colab VM (`T4`, `L4`, `A100`, or CPU) and execute a zero-install bundle directly from the terminal:
```bash
colab new -s gpu --gpu T4 --name nexus-job
colab run ./colab_job_bundle.py -s gpu
```

### Mode D: Multi-File Dataset Staging (`zlib` + `base64` Compression)
`pack_self_contained_script` automatically compresses staged `--file DEST=SRC` payloads with `zlib.compress(..., level=9)` + `base64`, allowing multi-megabyte scientific CSV/JSON datasets to be dispatched over `nexus_lite.colab.tunnel` (`POST /exec`, 1 MB body limit) or embedded into a single `--bootstrap-out` script.

---

## 4. Isolated Local Sandbox (`local`)

Verify that a workload runs cleanly in an isolated zero-install environment (`python -I` with empty `PYTHONPATH` and a temporary runtime directory) before dispatching remotely:

```bash
nexus-lite compute local --python "RESULT = {'version': nl.__version__, 'stm': len(nl.stm_list())}"
```
