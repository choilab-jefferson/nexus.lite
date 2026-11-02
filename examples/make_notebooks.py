#!/usr/bin/env python3
"""Generate the Colab notebooks: local-LLM swarms (T4 + A100/Pro) and the loom multi-agent build."""
import json, pathlib

ROOT = pathlib.Path(__file__).resolve().parent
HELPER = (ROOT / "ollama_agent.py").read_text()

def md(s): return {"cell_type": "markdown", "metadata": {}, "source": s.splitlines(True)}
def code(s): return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": s.splitlines(True)}

def build(title, gpu, env_note, models, env_vars, hetero, tiny=False):
    main = models[0]
    cells = [
        md(f"# {title}\n\nRuntime → **{gpu}**. {env_note}\nRuns Ollama on the Colab GPU and drives a parallel swarm + sequential pipeline of *local* model agents via `nexus_lite.run_swarm` / `run_pipeline`. No API keys."),
        md("## 1. GPU check + install"),
        code("!nvidia-smi --query-gpu=name,memory.total --format=csv\n!apt-get update -qq && apt-get install -y -qq dtach zstd\n!pip install -q git+https://github.com/choilab-jefferson/nexus.lite.git\n!curl -fsSL https://ollama.com/install.sh | sh"),
        md("## 2. Start Ollama (GPU) and pull model(s)"),
        code("import subprocess, time, urllib.request, os\n"
             f"MODELS = {models!r}\nMODEL = MODELS[0]\n"
             f"os.environ.update({env_vars!r})\n"
             'subprocess.Popen(["ollama", "serve"], stdout=open("/tmp/ollama.log", "w"), stderr=subprocess.STDOUT)\n'
             "for _ in range(60):\n    try: urllib.request.urlopen('http://127.0.0.1:11434'); break\n    except Exception: time.sleep(1)\n"
             "for m in MODELS:\n    subprocess.run(['ollama', 'pull', m], check=True)\n"
             "!ollama list"),
        md("## 3. Agent wrapper\n`run_swarm` feeds each spec's `prompt` to its `cmd` on **stdin**; this helper (also `examples/ollama_agent.py`) forwards it to the local model."),
        code("import pathlib\npathlib.Path('/content/nexus-agents').mkdir(exist_ok=True)\n"
             f"pathlib.Path('/content/nexus-agents/ollama_agent.py').write_text({HELPER!r})\n"
             "def agent(system, model=None):\n"
             "    return f'python3 /content/nexus-agents/ollama_agent.py --model {model or MODEL} --system \"{system}\"'\n"
             "print(subprocess.run(agent('Answer in one sentence.'), shell=True, input='What is a GPU?', text=True, capture_output=True).stdout)"),
    ]
    m2 = models[1] if hetero else None
    sw_models = ["None", "None", "None"] if not hetero else ["None", repr(m2), "None"]
    if tiny:
        roles = [("arch", "software architect", "key design decisions"), ("sec", "security reviewer", "security risks"),
                 ("tests", "QA engineer", "test cases"), ("perf", "performance engineer", "performance bottlenecks"),
                 ("ux", "API ergonomics reviewer", "usability problems"), ("ops", "SRE", "operational/monitoring concerns")]
        body = "".join(f"  {{'task_id': {t!r}, 'cmd': agent('You are a {r}. Be concise.', MODELS[{i % 2}]), 'prompt': f'List 3 {w} for {{topic}}.'}},\n" for i, (t, r, w) in enumerate(roles))
        cells += [
            md("## 4. Tiny-model swarm: 6 agents, 2 tiny models, one GPU (or CPU)\nAgents alternate between `" + models[0] + "` and `" + models[1] + "`; all 6 run concurrently."),
            code("import nexus_lite as nl\nfrom nexus_lite import colab\nfrom nexus_lite.demos import viz\ncolab.ensure_colab()\ntopic = 'a REST API for a todo-list service'\nspecs = [\n" + body + "]\nt0 = time.time()\nresults = nl.run_swarm(specs, caller='orchestrator', timeout=900.0)\nprint(f'6-agent swarm finished in {time.time()-t0:.1f}s')\nfor r in results: print(f\"\\n[{r['status'].upper()}] {r['task_id']}\\n{r['output'].strip()}\")"),
        ]
        cells += [
            md("## 5. Sequential pipeline: driver → verifier → documenter"),
            code("stages = [\n  {'name': 'driver',     'cmd': agent('You write short Python code.', MODELS[0]),        'prompt': 'Write a Python function slugify(text). Output only code.'},\n  {'name': 'verifier',   'cmd': agent('You review code for bugs. Be concise.', MODELS[1]), 'prompt': 'Review this code and list issues:\\n{prev_output}'},\n  {'name': 'documenter', 'cmd': agent('You write concise docs.', MODELS[0]),             'prompt': 'Write a 3-line docstring based on this review:\\n{prev_output}'},\n]\npipe = nl.run_pipeline(stages, initial_prompt='slugify', caller='orchestrator', timeout=900.0)\nfor s in pipe['stages']: print(f\"--- {s['stage']} (rc={s.get('rc')}) ---\\n{s.get('output','').strip()}\\n\")"),
            md("## 6. Inspect"),
            code("!nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv || true\n!ollama ps\nviz.dashboard()"),
        ]
        return {"cells": cells, "metadata": {"language_info": {"name": "python"}, "colab": {"provenance": [], "gpuType": "T4"}, "accelerator": "GPU", "kernelspec": {"name": "python3", "display_name": "Python 3"}}, "nbformat": 4, "nbformat_minor": 5}
    cells += [
        md("## 4. Parallel swarm: 3 local agents on one GPU" + (f"\nHeterogeneous: the security reviewer runs on `{m2}`, the others on `{main}` (both stay resident in VRAM)." if hetero else "\nOllama queues requests, so the workers share the GPU.")),
        code("import nexus_lite as nl\nfrom nexus_lite import colab\nfrom nexus_lite.demos import viz\ncolab.ensure_colab()\ntopic = 'a REST API for a todo-list service'\n"
             "specs = [\n"
             f"  {{'task_id': 'arch',  'cmd': agent('You are a software architect. Be concise.', {sw_models[0]}), 'prompt': f'List 4 key design decisions for {{topic}}.'}},\n"
             f"  {{'task_id': 'sec',   'cmd': agent('You are a security reviewer. Be concise.', {sw_models[1]}),  'prompt': f'List 4 security risks for {{topic}}.'}},\n"
             f"  {{'task_id': 'tests', 'cmd': agent('You are a QA engineer. Be concise.', {sw_models[2]}),        'prompt': f'List 4 test cases for {{topic}}.'}},\n"
             "]\nt0 = time.time()\nresults = nl.run_swarm(specs, caller='orchestrator', timeout=600.0)\n"
             "print(f'swarm finished in {time.time()-t0:.1f}s')\n"
             "for r in results: print(f\"\\n[{r['status'].upper()}] {r['task_id']}\\n{r['output'].strip()}\")"),
        md("## 5. Sequential pipeline: driver → verifier → documenter"),
        code("stages = [\n"
             "  {'name': 'driver',     'cmd': agent('You write short Python code.'),          'prompt': 'Write a Python function slugify(text). Output only code.'},\n"
             f"  {{'name': 'verifier',   'cmd': agent('You review code for bugs. Be concise.'{', ' + repr(m2) if hetero else ''}), 'prompt': 'Review this code and list issues:\\n{{prev_output}}'}},\n"
             "  {'name': 'documenter', 'cmd': agent('You write concise docs.'),               'prompt': 'Write a 3-line docstring based on this review:\\n{prev_output}'},\n"
             "]\npipe = nl.run_pipeline(stages, initial_prompt='slugify', caller='orchestrator', timeout=600.0)\n"
             "for s in pipe['stages']: print(f\"--- {s['stage']} (rc={s.get('rc')}) ---\\n{s.get('output','').strip()}\\n\")"),
        md("## 6. Inspect: GPU utilisation, loaded models, dashboard"),
        code("!nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv\n!ollama ps\nviz.dashboard()"),
    ]
    return {"cells": cells, "metadata": {"language_info": {"name": "python"}, "colab": {"provenance": [], "gpuType": gpu.split()[0]}, "accelerator": "GPU", "kernelspec": {"name": "python3", "display_name": "Python 3"}}, "nbformat": 4, "nbformat_minor": 5}

OUT = ROOT.parent / "notebooks"
nbs = {
    "local_llm_swarm_colab_t4.ipynb": build(
        "nexus-lite × Colab T4: local-LLM multi-agent swarm", "T4 (free tier OK)",
        "~15GB VRAM → one 12B model (4-bit ≈ 8GB).", ["gemma4:12b"],
        {"OLLAMA_NUM_PARALLEL": "2"}, False),
    "local_llm_swarm_colab_a100_pro.ipynb": build(
        "nexus-lite × Colab A100 (Pro): heterogeneous local-LLM swarm", "A100 (Colab Pro)",
        "40GB VRAM → Qwen 3.8 27B (≈18GB) + Gemma 4 12B (≈8GB) loaded together.", ["qwen3.8:27b", "gemma4:12b"],
        {"OLLAMA_NUM_PARALLEL": "3", "OLLAMA_MAX_LOADED_MODELS": "2"}, True),
}
nbs["local_llm_swarm_colab_tiny.ipynb"] = build(
    "nexus-lite × Colab: tiny-model swarm (CPU or T4)", "T4 (or CPU runtime)",
    "Two tiny models (~1-3GB each) → 6 concurrent agents; works on a CPU runtime too, just slower.",
    ["lfm3:1.7b", "gemma4:e2b"], {"OLLAMA_NUM_PARALLEL": "6", "OLLAMA_MAX_LOADED_MODELS": "2"}, True, tiny=True)

def loom_multi_agent_nb():
    cells = [
        md("# loom multi-agent build on Colab\n\n"
           "Multi-agent is only justified where one agent cannot do the job in one shot, so this notebook compares both honestly. "
           "An orchestrator splits a spec of four tricky python modules (tokenizer, intervals, TTL/LRU cache, money rounding) into "
           "worker subtasks. Workers write files into a persistent sandbox with loom's `write_file` tool. The harness runs "
           "**hidden** acceptance tests that live outside the sandbox and never appear in a prompt; workers only see failing "
           "test names and failure kinds (up to 3 review rounds, so at most 2 rework rounds). A single agent gets the same spec and the same hidden "
           "tests in one shot; its pass count is reported next to the multi-agent result.\n\n"
           "A failed orchestrator, worker or reviewer fails the run (never PASS). Each agent retries a bounded number of times, "
           "then falls back to the next provider; provider errors are summarised per agent with keys redacted.\n\n"
           "Add `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY` in the Colab Secrets panel (key icon, enable notebook access) for real "
           "models. Keys are read only via `google.colab.userdata`, passed to the loom child process, and never printed. "
           "With no key the offline mock path runs: loom's mock provider cannot call tools, so the harness writes scripted file "
           "payloads (two flawed modules fixed in round 2). Mock numbers are scripted, not evidence about any model."),
        md("## 1. Install nexus-lite"),
        code("!pip install -q git+https://github.com/choilab-jefferson/nexus.lite.git pytest"),
        md("## 2. Install and probe the `loom` binary\n"
           "Installing loom is an explicit step: `install_loom()` downloads the pinned release binary and verifies its sha256. "
           "Then `nexus_lite.loom.probe()` verifies the CLI capabilities and exit-code contract."),
        code("from nexus_lite import loom\n"
             "import os\n"
             "if not loom.find_loom():\n"
             "    p = loom.install_loom()  # explicit download of the pinned release (sha256-verified)\n"
             "    os.environ['LOOM_BIN'] = str(p)\n"
             "print('MIN_LOOM_VERSION:', loom.MIN_LOOM_VERSION, 'DEFAULT_EXIT_CODES:', loom.DEFAULT_EXIT_CODES)\n"
             "pr = loom.probe()\n"
             "print('probe:', pr.available, pr.path or pr.error)"),
        md("## 3. Core 4-module build (`basic`: tokenizer, intervals, ttlcache, money)\n"
           "The sandbox directory is kept and its path printed. Add `--json-out run.json` "
           "to keep rounds, per-agent status/time/error, hidden pass counts and baseline numbers."),
        code("from nexus_lite.demos.loom_multi_agent import main\nmain(['--json-out', '/tmp/loom_basic_run.json'])"),
        md("## 4. IBSI Radiomics multi-agent suite (`radiomics-firstorder`, `radiomics-texture`, `radiomics-stats`)\n"
           "Runs all three 4-module IBSI radiomics tasks (93 hidden acceptance tests total: intensity discretisation, "
           "3D ROI masking, first-order statistics, histogram entropy/uniformity, 2D/3D GLCM & GLRLM textures, "
           "Shrout-Fleiss ICC(3,1), Benjamini-Hochberg FDR, train-only Z-score standardisation, and Harrell's C-index)."),
        code("from nexus_lite.demos.loom_multi_agent import main\n"
             "from nexus_lite.demos.loom_radiomics_tasks import DEFAULT_TASKS\n"
             "main(['--task', DEFAULT_TASKS, '--json-out', '/tmp/loom_radiomics_run.json'])"),
        md("## 5. Failure injection, retry/fallback & secret redaction\n"
           "Force the offline path: `main(['--providers', 'mock'])`. Failure injection, e.g. a wrong key for one worker so retry/fallback "
           "run: `main(['--providers', 'anthropic,gemini', '--inject-bad-key', 'worker-2'])`; a short timeout: "
           "`'--inject-timeout', 'reviewer=1'`. Repeat runs: `'--runs', '3'`. Baseline self-repair, reported separately: "
           "`'--baseline-repair', '3'`."),
        code("from nexus_lite.demos.loom_multi_agent import redact\n"
             "sample_err = '401 Unauthorized: sk-ant-api03-SECRETKEY1234567890 and AIza' + 'SyD-SECRETGEMINIKEY1234567890'\n"
             "print('Redacted error sample:', redact(sample_err))"),
        md("## 6. Zero-preinstall remote compute bundle (`nexus_lite.compute` × Colab tunnel)\n"
           "Packs `nexus_lite` + the 3 IBSI radiomics `loom` multi-agent tasks into a single self-extracting script and "
           "runs it over an ephemeral `nexus_lite.colab.tunnel` loopback server (or exports a standalone Colab bundle)."),
        code("import nexus_lite as nl\n"
             "from nexus_lite.colab import tunnel as colab_tunnel\n\n"
             "info = colab_tunnel.start_tunnel(port=0, tunnel=False)\n"
             "try:\n"
             "    tok = colab_tunnel.token_file_path(info['port']).read_text().strip()\n"
             "    res = nl.run_on_colab(\n"
             "        python=\"\"\"\n"
             "from nexus_lite.demos.loom_multi_agent import build, get_task\n"
             "r = build(['mock'], '', {}, max_rounds=3, baseline=True, out=lambda *a, **k: None, backoff=0.0, task=get_task('radiomics-firstorder'))\n"
             "RESULT = {'ok': r['ok'], 'passed': r['hidden']['passed'], 'total': r['hidden']['total']}\n"
             "\"\"\",\n"
             "        url=info['local_url'],\n"
             "        token=tok,\n"
             "    )\n"
             "    print('Tunnel execution result:', res['result'])\n"
             "finally:\n"
             "    colab_tunnel.stop_tunnel()"),
    ]
    return {"cells": cells, "metadata": {"language_info": {"name": "python"}, "colab": {"provenance": []},
                                         "kernelspec": {"name": "python3", "display_name": "Python 3"}},
            "nbformat": 4, "nbformat_minor": 5}


nbs["loom_multi_agent_colab.ipynb"] = loom_multi_agent_nb()
for n, nb in nbs.items():
    (OUT / n).write_text(json.dumps(nb, indent=1))
    print("wrote", n)
