# Changelog

Release notes for nexus-lite. Each GitHub Release uses its version section.

## v0.2.2

### Added
- `nexus_lite.loom.install_loom` + CLI `nexus-lite loom install [--version V] [--dest PATH]`: explicit, opt-in install of the pinned released loom binary (`LOOM_VERSION`), sha256-verified against the release `checksums.txt`; never runs implicitly.
- Bus v1 (lite#8, schema v2): integer monotonic `messages.seq` (returned by `bus_send`), `bus_cursors` + `bus_ack`, `bus_register`/`bus_unregister` over `sessions` (new `last_seen`; `ALL` reaches only live registered sessions, TTL `LIVE_TTL_S`; unacked direct messages re-emitted once on register), `bus_read(since_seq=, unacked=)`, `bus_prune` + CLI `bus ack|register|unregister|prune`, control-char/type validation (`protocol.validate_bus_field`). Single-instance scope; additive.
- `nexus_lite.phi.phi_gate_error`: minimal opt-in PHI gate. With `NEXUS_SENSITIVITY=phi`, `core.delegate` refuses non-local runners (allowed: `ollama` except `launch`, `sh/bash/zsh/dash`, python) and forces `fallback_cmd=None`; `run_multi_tier_swarm(phi=True)` now builds Tier-0 as `ollama run <model>` (default `llama3.2`) instead of opencode. Unset/other value = no change. Stateless: no store, no endpoint probe (lite#24).
- `nexus_lite.provider_errors`: pure stdlib quota/auth classifier (`classify_provider_error` -> `rate_limit|quota|auth|None`, JSON error bodies, `is_quota_or_auth_failure`); `core` keeps private aliases. Marker lists merged with nexus.code's (lite#25).
- `nexus_lite.guard`: env-driven delegation depth / leaf guard primitive (`leaf_guard_error`, `current_delegation_depth`, `max_delegation_depth`); honors `NEXUS_MAX_DELEGATION_DEPTH`, `NEXUS_DELEGATED_ROLE`, `NEXUS_ALLOW_NESTED_MININEXUS`. nexus.code imports and extends it (lite#23).

- `nexus_lite.artifacts`: one task-artifact layout contract (file-name constants, `artifacts_root`, `task_dir`, `export_env`, `task_dir_from_env`). `delegate` now also writes `result.md` (same bytes as `output.md`) and `meta.json` `{task_id, agent, status, rc, started, finished}`; workers get the per-task dir as `NEXUS_TASK_DIR`; the artifact root is an argument (default `<workspace>/artifacts`, no env override) (lite#22).

### Changed
- `core.delegate` uses the guard instead of a hardcoded depth 2; default env behaviour unchanged, an unparseable `NEXUS_DELEGATION_DEPTH` is now refused instead of treated as 0 (lite#23).

### Breaking Changes
- **Colab Tunnel Relocation**: Relocated `nexus_lite.bridge` out of the core kernel into a standalone Colab utility `nexus_lite.colab.tunnel` (`ColabTunnelClient`, `start_tunnel`, `stop_tunnel`, `get_tunnel_info`, `is_tunnel_alive`).
- Dropped the `bridge` subcommand from the main `nexus-lite` CLI; exposed the utility as its own console script `nexus-lite-colab` and `python -m nexus_lite.colab.tunnel`.
- Core kernel no longer imports or lazy-loads the network service daemon (`nexus_lite.colab.tunnel`), adhering to the Open-Core-Kernel Unix CLI architecture (§2: no daemon in core). Removed all compatibility shims under the old `bridge` name (lite#19).
- Added: public `nexus_lite.pty.find_dtach_master_pid`, `PROVIDER_MANIFESTS`, `classify` (states `none|needs_input|working|idle`); `is_busy`/`is_idle` now derive from the manifests. `_find_dtach_pid` kept as an alias.
- Fixed: dtach master resolver no longer falls back to `matches[0]` when no/several candidates own children (ambiguous -> `None`).
- Added: public packet aliases `nexus_lite.pty.PKT`, `PUSH_MAX`, `packet`, `winch_packet` (private names kept) for nexus.code imports.

### Changed
- **Memory package** (lite#20): the three memory tiers are now separate modules under `nexus_lite.memory`: `stm` (was `nexus_lite.stm` plus `core.stm_*`), `mtm` (file/SQLite bus and inbox pointer delivery: `bus_send`, `bus_read`), `ltm` (markdown wiki plus FTS5: `wiki_write/read/search/list`). Workspace path and SQLite helpers moved to `nexus_lite._db` (still re-exported by `core`). No function renamed; CLI subcommands, SQLite tables, `.nexus/` layout, `stm/1` files and pointer lines are unchanged. Top-level `nexus_lite.bus_send` etc. keep working without warning.
- `import nexus_lite` is now lazy for `core`: only `pty`, `roles`, `protocol` load eagerly, so `import nexus_lite.memory.<tier>` pulls in no other tier, core, colab, demos or viz.

### Kernel/demo split (lite#10)
- **Breaking: CLI.** `demo`, `multi-tier`, `interact`, `cooperate` and `dashboard` left `nexus-lite`; they are now `nexus-lite-demo <subcommand>` (`python -m nexus_lite.demos`). `nexus-lite --help` lists kernel subcommands only.
- **Breaking: module paths.** `nexus_lite.demos` is now a package: `demos.scenarios` (old `demos.py`), `demos.orchestration` (`run_multi_tier_swarm`, `run_agent_interaction`, `run_cooperative_problem_solver`, formerly in `core`), `demos.viz`, `demos.loom_multi_agent|loom_multi_task|loom_radiomics_tasks`. The old `nexus_lite.viz` / `nexus_lite.loom_*` paths remain as deprecated shim modules. Colab helpers (`colab_sync_*`, `sync_dotdir_to_drive`, `ColabInitResult`, `detect_env`, `ensure_colab`, `ensure_env`) moved to `nexus_lite.colab.env` (also exported from `nexus_lite.colab`).
- **Breaking:** `get_db` and `get_db_path` are no longer in `nexus_lite.__all__` (import from `nexus_lite._db`). The `loom.py` adapter stays in the core namespace.
- `run_swarm(live_viz=...)` is ignored with a `DeprecationWarning` (the core renders no dashboards). The unused `max_turns` key in the multi-tier Tier-0 spec was removed.
- `nexus_lite.__version__` now comes from the installed package metadata (pyproject is the single source; falls back to an adjacent `pyproject.toml`).
- The core (`core`, `cli`, `memory`, ...) never imports `nexus_lite.demos` or `nexus_lite.colab`; a test enforces it. `ensure_opencode` stays in `core` because `run_swarm` uses it.

### Deprecated
- `NEXUS_ARTIFACT_DIR` (per-task dir env): still exported with the same value as `NEXUS_TASK_DIR` and accepted on read (canonical wins); removed after one release (lite#22).
- Old names `core.{run_multi_tier_swarm,run_agent_interaction,run_cooperative_problem_solver,colab_sync_to_drive,colab_sync_from_drive,sync_dotdir_to_drive,ColabInitResult,detect_env,ensure_colab,ensure_env,...}` and top-level `nexus_lite.{run_*_demo*,dashboard,render_dashboard_html,watch,ensure_env,ensure_colab,detect_env,sync_dotdir_to_drive,get_db,get_db_path,...}`: resolved lazily with a `DeprecationWarning`; removed after one release.
- `nexus_lite.stm` (import path), and `nexus_lite.core.{bus_send,bus_read,wiki_write,wiki_read,wiki_search,wiki_list,stm_write,stm_read,stm_list,stm_rebuild_index,stm_search,stm_pack}`: still work via thin aliases but emit `DeprecationWarning`; removed after one release. Use `nexus_lite.memory.*`.

## v0.2.1
- loom adapter: `crash` status for signal exits (rc<0); `bad_output` status when rc=0 but stdout is not JSON (was ok with `result=None`).
- loom multi-agent example: now a real build (orchestrator splits a spec, workers write files with loom `write_file` into a kept sandbox, reviewer plus harness run pytest, up to 3 review rounds (at most 2 rework rounds), single-agent baseline, per-agent table). Mock path is offline and deterministic (harness writes scripted payloads).
- `agent()` no longer raises on timeout/OSError; no longer passes `--max-total-tokens` (rejected by loom v0.1.1; loom v0.2.x accepts it, still not passed).
- loom multi-agent example, reworked: four-module spec with subtle traps (caseless identifiers, half-open intervals, TTL boundary, ties-to-even rounding); hidden acceptance tests outside the sandbox (never in prompts; workers see only failing test names and failure kinds); single-agent baseline scored on the same hidden tests (optional separate self-repair rounds).
- A failed orchestrator/worker/reviewer now fails the run (exit 1, `RESULT: FAIL (...)` line) instead of reporting PASS. Per-agent stderr summaries with key redaction (`sk-ant-`, `AIza`, `*_API_KEY=`, bearer tokens, exact secret values); new `auth` status.
- Bounded retries (`--retries`, none for auth errors) and provider fallback; per-agent failure injection (`--inject-bad-key ROLE`, `--inject-timeout ROLE=SEC`) with deterministic mock equivalents (`mock,mock2` providers).
- Default loom is release `v0.2.1` (`LOOM_TAG`, override via env; checksum-verified `loom-linux-amd64`, source-build fallback). v0.2.1 adds `usage` in `--json`, recoverable policy-violation tool errors and an 8192 default output cap; a flag named only `--key` is not yet redacted in trace argv.
- Observability and CLI: loom version (or `n/a (reason)`), tokens or `n/a (reason)`, `--runs N`, `--json-out`, `--log-file`. `examples/loom_multi_agent.py` prefers the checkout over an older installed nexus-lite and prints an install hint if the module is missing.
- Tests: failure injection (bad/missing key, crash, timeout, bad output, provider fallback, checksum mismatch, key hygiene); loom tests added to GitLab and GitHub CI.

## v0.2.0
- Colab multi-agent notebook and loom adapter.
