#!/usr/bin/env python3
"""Generate the Colab local-LLM swarm notebooks (T4 + A100/Pro)."""
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
            code("import nexus_lite as nl\nnl.ensure_colab()\ntopic = 'a REST API for a todo-list service'\nspecs = [\n" + body + "]\nt0 = time.time()\nresults = nl.run_swarm(specs, caller='orchestrator', timeout=900.0)\nprint(f'6-agent swarm finished in {time.time()-t0:.1f}s')\nfor r in results: print(f\"\\n[{r['status'].upper()}] {r['task_id']}\\n{r['output'].strip()}\")"),
        ]
        cells += [
            md("## 5. Sequential pipeline: driver → verifier → documenter"),
            code("stages = [\n  {'name': 'driver',     'cmd': agent('You write short Python code.', MODELS[0]),        'prompt': 'Write a Python function slugify(text). Output only code.'},\n  {'name': 'verifier',   'cmd': agent('You review code for bugs. Be concise.', MODELS[1]), 'prompt': 'Review this code and list issues:\\n{prev_output}'},\n  {'name': 'documenter', 'cmd': agent('You write concise docs.', MODELS[0]),             'prompt': 'Write a 3-line docstring based on this review:\\n{prev_output}'},\n]\npipe = nl.run_pipeline(stages, initial_prompt='slugify', caller='orchestrator', timeout=900.0)\nfor s in pipe['stages']: print(f\"--- {s['stage']} (rc={s.get('rc')}) ---\\n{s.get('output','').strip()}\\n\")"),
            md("## 6. Inspect"),
            code("!nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv || true\n!ollama ps\nnl.dashboard()"),
        ]
        return {"cells": cells, "metadata": {"language_info": {"name": "python"}, "colab": {"provenance": [], "gpuType": "T4"}, "accelerator": "GPU", "kernelspec": {"name": "python3", "display_name": "Python 3"}}, "nbformat": 4, "nbformat_minor": 5}
    cells += [
        md("## 4. Parallel swarm: 3 local agents on one GPU" + (f"\nHeterogeneous: the security reviewer runs on `{m2}`, the others on `{main}` (both stay resident in VRAM)." if hetero else "\nOllama queues requests, so the workers share the GPU.")),
        code("import nexus_lite as nl\nnl.ensure_colab()\ntopic = 'a REST API for a todo-list service'\n"
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
        code("!nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv\n!ollama ps\nnl.dashboard()"),
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
for n, nb in nbs.items():
    (OUT / n).write_text(json.dumps(nb, indent=1))
    print("wrote", n)
