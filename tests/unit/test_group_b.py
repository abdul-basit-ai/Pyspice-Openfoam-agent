"""Group B (remain_plan): Phase 2 parser units/ranges, Phase 22 manifest
wiring, Phase 18 provenance logging, Phase 4 Design persistence."""

from __future__ import annotations

import json

import pytest

from pyspice_openfoam_agent.bundle.manifest import (
    build_manifest_from_artifacts,
    validate_manifest,
)
from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.orchestrator.graph import (
    AgentConfig,
    _call_openrouter,
    make_graph,
)
from pyspice_openfoam_agent.orchestrator.tools import ToolContext
from pyspice_openfoam_agent.sizing.spec_parser import SpecParseError, parse_spec


# ---------------- B1: parser units & ranges ----------------

def test_input_range_labeled_form():
    r = parse_spec("Vin = 36-60 V, Vout = 12 V, Iout = 20 A")
    q = r.requirements
    assert (q.vin_min, q.vin_max) == (36.0, 60.0)
    assert q.Vin == 60.0  # worst-case voltage-stress endpoint
    assert any("input range 36" in a for a in r.assumptions)


def test_input_range_unlabeled_and_to_forms():
    for text, lo, hi in [
        ("input 12-36 V, output 5 V, 3 A", 12.0, 36.0),
        ("12-36 V input, 5 V output at 3 A", 12.0, 36.0),
        ("input 12 to 36 V, output 5 V, 3 A", 12.0, 36.0),
    ]:
        q = parse_spec(text).requirements
        assert (q.vin_min, q.vin_max) == (lo, hi), text
        assert q.Vin == hi, text


def test_range_survives_requirements_roundtrip():
    q = parse_spec("Vin = 36-60 V, Vout = 12 V, Iout = 20 A").requirements
    assert q.vin_min == 36.0 and q.vin_max == 60.0
    # defaults stay None for a fixed-Vin spec
    q2 = parse_spec("12 V to 5 V at 5 A").requirements
    assert q2.vin_min is None and q2.vin_max is None


def test_fsw_mhz_and_plain_hz_units():
    assert parse_spec(
        "12 V to 5 V at 3 A, 2 MHz").requirements.fsw_khz == 2000.0
    assert parse_spec(
        "12 V to 5 V at 3 A, 500000 Hz").requirements.fsw_khz == 500.0
    assert parse_spec(
        "12 V to 5 V at 3 A, 300 kHz").requirements.fsw_khz == 300.0


def test_ripple_volts_unit_not_silently_replaced():
    q = parse_spec("12 V to 5 V at 5 A, ripple < 0.2 V").requirements
    assert q.ripple_v == 0.2
    # mV still wins over V
    assert parse_spec(
        "12 V to 5 V at 5 A, ripple < 100 mV").requirements.ripple_v == 0.1


def test_efficiency_phrasings():
    for text in ["efficiency should exceed 95%",
                 "efficiency > 92.5%",
                 "95% efficiency"]:
        eff = parse_spec(f"12 V to 5 V at 5 A, {text}").requirements.efficiency_target
        assert eff is not None and 0.9 < eff <= 1.0, text


def test_contradictions_protection_sanity():
    with pytest.raises(SpecParseError, match="UVLO"):
        parse_spec("12 V to 5 V at 5 A, UVLO 18 V")
    with pytest.raises(SpecParseError, match="OCP"):
        parse_spec("12 V to 5 V at 5 A, OCP 3 A")
    with pytest.raises(SpecParseError, match="cannot be equal"):
        parse_spec("input 5 V, output 5 V, 1 A")


def test_tj_default_is_a_documented_assumption():
    r = parse_spec("12 V to 5 V at 5 A")
    assert any("Tj limit not specified" in a for a in r.assumptions)


# ---------------- B2 + B5: mock graph run -> design.json + manifest ----------------

def _mock_run(run_dir):
    responses = [
        {"tool_calls": [{"name": "size_converter",
                         "args": {"Vin": 12.0, "Vout": 5.0, "Iout": 5.0,
                                  "fsw_khz": 500.0, "Vripple": 0.05}}]},
        {"tool_calls": [{"name": "select_components", "args": {}}]},
        {"done": True, "final": {"summary": "sized and selected"}},
    ]
    config = AgentConfig(run_dir=run_dir, max_steps=6)
    ctx = ToolContext(run_dir=run_dir, library=load_library())
    graph = make_graph(config, ctx, mock_responses=responses)
    state = {"task": {"Vin": 12, "Vout": 5, "Iout": 5, "fsw_khz": 500},
             "transcript": [], "tool_calls": [], "step": 0,
             "done": False, "final": None, "error": None}
    return graph.invoke(state)


def test_finalize_writes_design_json(tmp_path):
    from pyspice_openfoam_agent.design.object import Design

    run_dir = tmp_path / "run1"
    result = _mock_run(run_dir)
    assert result["final"].get("design_json")
    d = Design.load(run_dir / "design.json")  # schema_version-checked reload
    assert d.requirements.Vin == 12.0 and d.requirements.Vout == 5.0
    assert d.topology.name == "buck"
    assert d.components.mosfet is not None
    assert d.components.mosfet.part_number == "BSC014N04LS"
    assert d.parameters.duty_cycle == pytest.approx(5.0 / 12.0, abs=0.05)


def test_finalize_writes_manifest_with_ics(tmp_path):
    run_dir = tmp_path / "run1"
    result = _mock_run(run_dir)
    assert result["final"].get("manifest")
    m = json.loads((run_dir / "manifest.json").read_text())
    validate_manifest(m)
    # no thermal outcome in this mock run -> deliberately infeasible, valid
    assert m["status"] == "infeasible"
    cats = {c["category"] for c in m["components"]}
    assert {"mosfet", "inductor", "capacitor", "gate_driver"} <= cats


def test_manifest_carries_new_verification_summaries():
    arts = {
        "sizing": {"topology": "buck"},
        "components": {"mosfet": "X", "inductor": "Y", "capacitor": "Z",
                       "gate_driver": "G", "controller": "C"},
        "spice": {"efficiency": 0.91},
        "control_loop": {"passed": True},
        "step_tests": {"load_step": {"undershoot_V": 0.4}},
        "sweep": {"mode": "load"},
        "electro_thermal": {"converged": True},
        "thermal": {"tj_per_device_C": {"hs_mosfet": 50.0},
                    "validation": {"nan_residuals": "PASS"}},
    }
    m = build_manifest_from_artifacts(arts, {"Vin": 12}, "feasible")
    d = m.to_dict()
    assert "control_loop" in d["electrical"] and "step_tests" in d["electrical"]
    assert d["thermal"]["validation"]["nan_residuals"] == "PASS"
    assert {c["category"] for c in d["components"]} == {
        "mosfet", "inductor", "capacitor", "gate_driver", "controller"}


# ---------------- B3: provenance logging ----------------

class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_call_openrouter_sends_temperature_and_logs(tmp_path, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["payload"] = json.loads(req.data.decode())
        return _FakeResponse({
            "choices": [{"message": {"content": "done", "tool_calls": None}}],
            "usage": {"total_tokens": 42},
        })

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    prov = tmp_path / "llm_provenance.jsonl"

    state = {"task": {"Vin": 12}, "transcript": [], "step": 3}
    out = _call_openrouter("test/model", state, provenance_path=prov)

    # sampling settings are SENT (goals Phase 0/18: deterministic-by-config)
    assert captured["payload"]["temperature"] == 0.0
    assert captured["payload"]["model"] == "test/model"
    assert out["done"] is True
    # and the per-call provenance record is on disk
    lines = prov.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["model"] == "test/model" and rec["temperature"] == 0.0
    assert rec["step"] == 3 and rec["usage"]["total_tokens"] == 42
    assert rec["response"]["final"]["summary"] == "done"
