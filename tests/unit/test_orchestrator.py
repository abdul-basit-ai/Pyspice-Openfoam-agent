"""Phase 11 + 11a + 12 tests: orchestrator (mock Gemini), design memory, manifest.

User decision: always-LLM, recorded mocks in tests — the graph runs for real
(LangGraph nodes, dispatch, memory writes) but `reason` replays scripted
Gemini responses instead of calling the API.
"""

from __future__ import annotations

import json

import pytest

from pyspice_openfoam_agent.bundle.manifest import (
    ManifestError,
    build_manifest_from_artifacts,
    validate_manifest,
    write_bundle,
)
from pyspice_openfoam_agent.orchestrator.graph import AgentConfig, make_graph
from pyspice_openfoam_agent.orchestrator.memory.design_memory import DesignMemory
from pyspice_openfoam_agent.orchestrator.tools import ToolContext, spec_signature
from pyspice_openfoam_agent.library.loader import load_library


@pytest.fixture
def run_dir(tmp_path):
    return tmp_path / "run1"


# ---------- Phase 11a: design memory ----------


def test_memory_roundtrip(tmp_path) -> None:
    mem = DesignMemory(tmp_path)
    mem.record_outcome(12, 5, 5, 500, {"Vin": 12}, {"mosfet": "BSC014N04LS"},
                       {"tj_max_C": 31.2, "converged": True})
    hits = mem.lookup(12, 5, 5, 500)
    assert len(hits) == 1
    assert hits[0]["best_config"]["mosfet"] == "BSC014N04LS"
    assert hits[0]["outcome"]["tj_max_C"] == 31.2


def test_memory_dedup_by_signature(tmp_path) -> None:
    mem = DesignMemory(tmp_path)
    mem.record_outcome(12, 5, 5, 500, {}, {}, {"run": 1})
    mem.record_outcome(12, 5, 5, 500, {}, {}, {"run": 2})
    # same spec class: attempts append (history preserved), signature stable
    assert spec_signature(12, 5, 5, 500) == spec_signature(12, 5, 5, 500.0)
    hits = mem.lookup(12, 5, 5, 500)
    assert len(hits) == 2
    # different spec class: separate record
    mem.record_outcome(48, 12, 2, 300, {}, {}, {"run": 3})
    assert len(mem.lookup(48, 12, 2, 300)) == 1


def test_memory_lookup_miss(tmp_path) -> None:
    mem = DesignMemory(tmp_path)
    assert mem.lookup(1, 2, 3, 400) == []


# ---------- Phase 12: manifest ----------


def test_manifest_valid_roundtrip(tmp_path) -> None:
    artifacts = {
        "sizing": {"topology": "buck"},
        "components": {"mosfet": "BSC014N04LS", "inductor": "XAL1010-472ME",
                       "capacitor": "GRM31CR61E476ME15"},
        "spice": {"converged": True, "ripple_V": 0.01, "efficiency": 0.9},
        "thermal": {"tj_max_C": 31.2, "converged": True, "case_dir": "/x"},
        "netlist": "/x/design.cir",
    }
    m = build_manifest_from_artifacts(artifacts, {"Vin": 12, "Vout": 5}, "feasible")
    d = m.to_dict()
    validate_manifest(d)  # must not raise
    path = write_bundle(m, tmp_path)
    loaded = json.loads(path.read_text())
    assert loaded["status"] == "feasible"
    assert len(loaded["components"]) == 3


def test_manifest_infeasible_status_valid(tmp_path) -> None:
    m = build_manifest_from_artifacts({"components": {}}, {}, "infeasible",
                                      notes=["all levers plateaued"])
    d = m.to_dict()
    validate_manifest(d)
    assert d["status"] == "infeasible"


def test_manifest_rejects_bad_status() -> None:
    with pytest.raises(ManifestError, match="bad status"):
        validate_manifest({"version": 1, "spec": {}, "status": "bogus",
                           "components": [], "electrical": {}, "thermal": {}, "artifacts": {}})


def test_manifest_rejects_missing_key() -> None:
    with pytest.raises(ManifestError, match="missing required key"):
        validate_manifest({"version": 1, "spec": {}, "status": "feasible"})


# ---------- Phase 11: graph (mock Gemini) ----------


def _mock_size_then_stop():
    """A recorded session: size -> select -> stop (small, fast, no CHT)."""
    return [
        {"tool_calls": [{"name": "size_converter",
                         "args": {"Vin": 12.0, "Vout": 5.0, "Iout": 5.0,
                                  "fsw_khz": 500.0, "Vripple": 0.05}}]},
        {"tool_calls": [{"name": "select_components", "args": {}}]},
        {"done": True, "final": {"summary": "sized and selected"}},
    ]


def test_graph_mock_run_completes(run_dir) -> None:
    config = AgentConfig(run_dir=run_dir, max_steps=6)
    ctx = ToolContext(run_dir=run_dir, library=load_library())
    graph = make_graph(config, ctx, mock_responses=_mock_size_then_stop())
    state = {
        "task": {"Vin": 12, "Vout": 5, "Iout": 5, "fsw_khz": 500},
        "transcript": [], "tool_calls": [], "step": 0,
        "done": False, "final": None, "error": None,
    }
    result = graph.invoke(state)
    assert result["done"]
    assert result["final"]["summary"] == "sized and selected"
    # artifacts accumulated through the real dispatch
    assert result["final"]["artifacts"]["sizing"]["topology"] == "buck"
    assert result["final"]["artifacts"]["components"]["mosfet"] == "BSC014N04LS"
    # transcript has one tool observation per call
    tool_msgs = [m for m in result["transcript"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert all(m["ok"] for m in tool_msgs)


def test_graph_tool_error_is_observed_not_fatal(run_dir) -> None:
    """ReAct contract: a tool error returns to the LLM as an observation."""
    responses = [
        {"tool_calls": [{"name": "select_components", "args": {}}]},  # wrong order: no sizing yet
        {"done": True, "final": {"summary": "recovered after error"}},
    ]
    config = AgentConfig(run_dir=run_dir, max_steps=4)
    ctx = ToolContext(run_dir=run_dir, library=load_library())
    graph = make_graph(config, ctx, mock_responses=responses)
    state = {"task": {}, "transcript": [], "tool_calls": [], "step": 0,
             "done": False, "final": None, "error": None}
    result = graph.invoke(state)
    tool_msgs = [m for m in result["transcript"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert not tool_msgs[0]["ok"]
    assert "size_converter first" in tool_msgs[0]["result"]["error"]
    assert result["done"]  # agent recovered and stopped cleanly


def test_graph_max_steps_guard(run_dir) -> None:
    """The loop refuses to run forever: max_steps triggers a clean stop."""
    endless = [{"tool_calls": [{"name": "select_components", "args": {}}]}] * 10
    config = AgentConfig(run_dir=run_dir, max_steps=3)
    ctx = ToolContext(run_dir=run_dir, library=load_library())
    graph = make_graph(config, ctx, mock_responses=endless)
    state = {"task": {}, "transcript": [], "tool_calls": [], "step": 0,
             "done": False, "final": None, "error": None}
    result = graph.invoke(state)
    assert result["step"] <= 3
    assert result.get("error") == "max steps reached" or result["done"]


def test_memory_written_by_finalize(run_dir) -> None:
    """Phase 11a: finalize records the outcome into design memory."""
    config = AgentConfig(run_dir=run_dir, max_steps=6)
    ctx = ToolContext(run_dir=run_dir, library=load_library())
    graph = make_graph(config, ctx, mock_responses=_mock_size_then_stop())
    state = {"task": {"Vin": 12, "Vout": 5}, "transcript": [], "tool_calls": [],
             "step": 0, "done": False, "final": None, "error": None}
    graph.invoke(state)
    # run_dir is tmp; DesignMemory anchors at parent.parent — use the same
    # resolution as the store to find the file
    mem = DesignMemory(run_dir)
    hits = mem.lookup(12, 5, 5, 500)
    assert len(hits) == 1  # finalize wrote the attempt
