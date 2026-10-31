"""Contract test against a REAL loom binary.

Skipped when no binary is found (LOOM_BIN, else PATH). The pinned constants
below mirror nexus.loom docs/contract.md; bump them together with that doc.
"""
import json
import os

import pytest

from nexus_lite.loom import DEFAULT_EXIT_CODES, LoomAdapter, find_loom, probe

CAPABILITIES_SCHEMA_VERSION = 1
TASKRESULT_SCHEMA_VERSION = 1
TRACE_SCHEMA_VERSION = 1

LOOM = find_loom()
pytestmark = pytest.mark.skipif(not LOOM, reason="no loom binary (set LOOM_BIN or add to PATH)")


def test_capabilities_contract():
    pr = probe(LOOM)
    assert pr.available, pr.error
    caps = pr.capabilities
    assert isinstance(caps, dict)
    assert caps["schema_version"] == CAPABILITIES_SCHEMA_VERSION
    codes = caps["exit_codes"]
    assert isinstance(codes, dict)
    assert {0, 1, 2, 3, 4, 130} <= set(codes.values())
    assert pr.version_ok


def test_run_prompt_mock_result_and_trace(tmp_path):
    trace = tmp_path / "trace.jsonl"
    res = LoomAdapter(bin=LOOM, timeout=60).run_prompt(
        "hi", provider="mock", trace_path=str(trace))
    assert res.returncode == 0, res.stderr
    assert res.ok and res.status == "ok"
    assert isinstance(res.result, dict), res.stdout
    assert res.result.get("exit_code") == 0
    assert res.result["schema_version"] == TASKRESULT_SCHEMA_VERSION
    lines = [ln for ln in trace.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "empty trace"
    events = [json.loads(ln) for ln in lines]  # every line must be valid JSON
    assert all(isinstance(e, dict) for e in events)
    assert len(res.trace) == len(events)
    assert all(e.get("schema_version") == TRACE_SCHEMA_VERSION for e in events)


def test_exit_code_table_matches_adapter_defaults():
    codes = LoomAdapter(bin=LOOM).exit_codes()
    for name, code in DEFAULT_EXIT_CODES.items():
        assert codes[name] == code
