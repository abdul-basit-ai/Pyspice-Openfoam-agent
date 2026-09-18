"""Tests for the goals-rework: P1 schema, P2 parser, P3 topology, P4 Design,
P5 netlist validation, P6 screening, P10 mesh presets, P12 validation,
P13 Pareto optimization."""

from __future__ import annotations

import json

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.design.object import (
    Design, DesignError, Requirements, SCHEMA_VERSION,
)
from pyspice_openfoam_agent.sizing.spec_parser import (
    SpecParseError,
    format_questions,
    parse_spec,
)
from pyspice_openfoam_agent.sizing.engine import Spec, size
from pyspice_openfoam_agent.netlist.selector import select_components
from pyspice_openfoam_agent.sizing.topology_select import evaluate_topologies, is_ambiguous, recommend_topology
from pyspice_openfoam_agent.sizing.screening import screen
from pyspice_openfoam_agent.netlist.validate import validate_netlist


# ---------- P1: extended schema ----------


@pytest.fixture(scope="module")
def lib():
    return load_library()


def test_p1_mosfet_tempco_method(lib):
    m = lib.mosfets["BSC014N04LS"]
    # default tempco 6000 ppm/K: at 125 degC (100 K above 25), factor 1.6
    assert m.rds_on_at(125.0) == pytest.approx(m.Rds_on * 1.6)
    assert m.rds_on_at(25.0) == m.Rds_on


def test_p1_inductor_tempco_method(lib):
    ind = lib.inductors["XAL1010-472ME"]
    assert ind.dcr_at(100.0) == pytest.approx(ind.DCR * (1 + 0.0039 * 75))


def test_p1_new_categories_optional(tmp_path):
    """The IC/diode categories stay OPTIONAL: a data dir without those YAMLs
    loads successfully with zero counts (verified on a stripped temp copy —
    the real library now ships populated files, Group A1)."""
    import shutil

    from pyspice_openfoam_agent.library.loader import LIBRARY_DIR, load_library

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for f in ("mosfets.yaml", "inductors.yaml", "capacitors.yaml"):
        shutil.copy(LIBRARY_DIR / f, data_dir / f)
    lib = load_library(data_dir)
    counts = lib.counts()
    assert counts["gate_drivers"] == 0
    assert counts["controllers"] == 0
    assert counts["diodes"] == 0
    # and the real library has them populated
    counts_full = load_library().counts()
    assert counts_full["gate_drivers"] >= 3 and counts_full["controllers"] >= 4


def test_p1_schema_new_fields(lib):
    m = lib.mosfets["BSC014N04LS"]
    assert hasattr(m, "Coss") and hasattr(m, "Crss")
    assert m.Rds_on_tempco_ppm > 0
    ind = lib.inductors["XAL1010-472ME"]
    assert ind.DCR_tempco_ppm > 0


# ---------- P2: NL spec parser ----------


def test_p2_parses_full_spec():
    r = parse_spec("Design a 12 V to 5 V 5 A converter at 500 kHz, "
                   "ripple < 50 mV, efficiency > 90%, Tj < 100 °C, "
                   "OCP 7 A, OTP 120 °C, UVLO 8 V")
    assert r.requirements.Vin == 12.0
    assert r.requirements.Vout == 5.0
    assert r.requirements.Iout == 5.0
    assert r.requirements.fsw_khz == 500.0
    assert r.requirements.ripple_v == pytest.approx(0.05)
    assert r.requirements.efficiency_target == pytest.approx(0.90)
    assert r.requirements.tj_max_c == 100.0
    assert r.requirements.ocp_a == 7.0
    assert r.requirements.otp_c == 120.0
    assert r.requirements.uvlo_v == 8.0
    assert r.requirements.unresolved_safety_items == []
    assert r.missing_safety == []


def test_p2_flags_missing_safety():
    r = parse_spec("48 V to 12 V at 20 A, 300 kHz, ripple < 100 mV")
    assert set(r.missing_safety) == {"OCP threshold", "OTP shutdown temperature",
                                     "UVLO threshold"}
    assert r.requirements.unresolved_safety_items == r.missing_safety


def test_p2_ripple_default_provisional():
    r = parse_spec("36 V to 12 V at 10 A")  # no ripple, no fsw
    # 1% of Vout provisional recommendation (goals' example)
    assert r.requirements.ripple_v == pytest.approx(0.12)
    assert any("1% of Vout" in a for a in r.assumptions)
    assert "ripple" in r.missing_material


def test_p2_contradiction_rejected():
    with pytest.raises(SpecParseError, match="impossible"):
        parse_spec("5 V to 5 V at 1 A, efficiency > 105%")


def test_p2_missing_required_raises():
    with pytest.raises(SpecParseError, match="Vout not found"):
        parse_spec("12 V input at 5 A")


def test_p2_format_questions():
    r = parse_spec("12 V to 5 V at 5 A")
    q = format_questions(r)
    assert "OCP" in q or "safety" in q.lower()
    assert "1% of Vout" in q


# ---------- P3: multi-criteria topology ----------


def test_p3_buck_recommended_for_stepdown():
    req = Requirements(Vin=12, Vout=5, Iout=5, fsw_khz=500, ripple_v=0.05)
    name, cands = recommend_topology(req)
    assert name == "buck"
    assert not is_ambiguous(cands)


def test_p3_all_candidates_have_rationale():
    req = Requirements(Vin=12, Vout=5, Iout=5, fsw_khz=500, ripple_v=0.05)
    for c in evaluate_topologies(req):
        assert c.pros and c.cons  # explanation contract
        assert c.switch_count > 0


# ---------- P4: Design object ----------


def test_p4_serialize_reload_regen(tmp_path):
    d = Design()
    d.requirements = Requirements(Vin=12, Vout=5, Iout=5, fsw_khz=500, ripple_v=0.05)
    d.topology.name = "buck"
    d.topology.rationale = "step-down, fewest parts"
    d.record_decision("topology=buck", reason="step-down", constraints_considered="ratio",
                      alternatives="boost, buck-boost", assumptions="CCM")
    path = d.save(tmp_path / "design.json")
    loaded = Design.load(path)
    assert loaded.topology.name == "buck"
    assert loaded.topology.rationale == "step-down, fewest parts"
    assert loaded.provenance[0]["decision"] == "topology=buck"
    assert loaded.schema_version == SCHEMA_VERSION


def test_p4_prior_version_rejected_then_marked(tmp_path):
    raw = json_dumps_old()
    p = tmp_path / "old.json"
    p.write_text(raw)
    # strict load refuses
    with pytest.raises(DesignError, match="Migrate explicitly"):
        Design.load(p)
    # explicit prior-version load marks provenance
    d, ver = Design.load_prior_version(p)
    assert ver == SCHEMA_VERSION - 1
    assert any(e["event"] == "loaded_prior_version" for e in d.provenance)


def test_p4_newer_schema_rejected(tmp_path):
    newer = json.dumps({"schema_version": SCHEMA_VERSION + 5,
                        "design": {"future_field": 1}})
    p = tmp_path / "new.json"
    p.write_text(newer)
    with pytest.raises(DesignError, match="Migrate explicitly"):
        Design.load(p)


def json_dumps_old() -> str:
    import json
    old_version = SCHEMA_VERSION - 1
    return json.dumps({
        "schema_version": old_version,
        "design": {
            "schema_version": old_version,
            "requirements": {"Vin": 12, "Vout": 5, "Iout": 5, "fsw_khz": 500,
                             "ripple_v": 0.05},
            "topology": {"name": "buck"},
        },
    })


def test_p4_validate_catches_divergence():
    d = Design()
    d.requirements = Requirements(Vin=12, Vout=5, Iout=5, fsw_khz=500, ripple_v=0.05)
    d.topology.name = "boost"  # contradictory with Vout<Vin
    d.netlist_path = "/fake/design.cir"  # netlist without components
    problems = d.validate()
    assert any("boost" in p for p in problems)
    assert any("diverged" in p for p in problems)


# ---------- P5: netlist connectivity ----------


def test_p5_valid_netlist_passes(tmp_path):
    cir = tmp_path / "good.cir"
    cir.write_text("""* buck
Vin in 0 DC 12
Shs in sw gate_hs 0 HS_MOD
Sls sw 0 gate_ls 0 LS_MOD
.model HS_MOD SW(Ron=0.0014 Roff=1e9 Vt=2.5 Vh=0.1)
.model LS_MOD SW(Ron=0.0014 Roff=1e9 Vt=2.5 Vh=0.1)
Vgate_hs gate_hs 0 DC 5
Vgate_ls gate_ls 0 DC 0
Lout sw lx 4.7u
Rdcr lx out 0.0143
Cout out out_esr 47u
Resrout out_esr 0 0.003
Rload out 0 1
.end
""")
    r = validate_netlist(cir)
    assert r.valid, f"floating: {r.floating_nodes}"


def test_p5_floating_node_detected(tmp_path):
    cir = tmp_path / "floating.cir"
    cir.write_text("""* broken: node 'orphan' has one connection
Vin in 0 DC 12
R1 in out 1k
R2 out 0 1k
R3 orphan out 1k
.end
""")
    r = validate_netlist(cir)
    assert not r.valid
    assert "orphan" in r.floating_nodes


# ---------- P6: fast screening ----------


def test_p6_passes_healthy_design(lib):
    spec = Spec(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3,
                ripple_ratio=0.40, Vripple=0.05)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    v = screen(12.0, 5.0, 5.0, 500e3, sizing, sel.mosfet, sel.inductor, sel.capacitor)
    assert v.passed, f"reasons: {v.reasons}"


def test_p6_rejects_undersized_mosfet(lib):
    spec = Spec(Vin=60.0, Vout=5.0, Iout=5.0, fsw=500e3,
                ripple_ratio=0.40, Vripple=0.05)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    # force a 25V MOSFET into a 60V design: screening must reject
    v = screen(60.0, 5.0, 5.0, 500e3, sizing, lib.mosfets["SISS44DN10"],
               sel.inductor, sel.capacitor)
    assert v.rejected
    assert any("Vds_max" in r for r in v.reasons)


# ---------- P10: mesh presets ----------


def test_p10_presets_change_resolution(lib):
    from pyspice_openfoam_agent.thermal.board import build_board_geometry
    from pyspice_openfoam_agent.thermal.mesh_generator import plan_mesh

    geo = build_board_geometry(lib.mosfets["BSC014N04LS"])
    fast = plan_mesh(geo, fidelity="fast")
    high = plan_mesh(geo, fidelity="high")
    assert high.n_cells > fast.n_cells * 2  # high must be substantially finer


def test_p10_unknown_preset_rejected(lib):
    from pyspice_openfoam_agent.thermal.board import build_board_geometry
    from pyspice_openfoam_agent.thermal.mesh_generator import plan_mesh, MeshPlanError

    geo = build_board_geometry(lib.mosfets["BSC014N04LS"])
    with pytest.raises(MeshPlanError, match="unknown fidelity"):
        plan_mesh(geo, fidelity="ultra")


# ---------- P12: thermal validation ----------


def test_p12_converged_result_valid():
    from pyspice_openfoam_agent.thermal.validation import validate_cht_result

    log = ("Time = 1\nDILUPBiCGStab: Solving for Ux, Initial residual = 0.1, "
           "Final residual = 0.001, No Iterations 5\n"
           "Time = 2\nDILUPBiCGStab: Solving for Ux, Initial residual = 0.01, "
           "Final residual = 0.0001, No Iterations 3\n")
    v = validate_cht_result(log, tj_max_k=310.0, power_in_w=2.6, ambient_k=300.0)
    assert v.converged
    assert v.valid
    assert v.energy_balance_ok is True


def test_p12_nan_result_invalid():
    from pyspice_openfoam_agent.thermal.validation import validate_cht_result

    log = ("Solving for p_rgh, Initial residual = nan, Final residual = nan\n"
           "Solving for Ux, Initial residual = nan, Final residual = nan\n")
    v = validate_cht_result(log, tj_max_k=None, power_in_w=2.6)
    assert not v.valid
    assert not v.converged


def test_p12_energy_balance_catches_bogus_tj():
    from pyspice_openfoam_agent.thermal.validation import validate_cht_result

    log = "Solving for Ux, Initial residual = 0.1, Final residual = 0.001\n"
    # 0.5W with Tj_max 500K => R_eff = 260 K/W, far outside [0.5, 100]
    v = validate_cht_result(log, tj_max_k=500.0, power_in_w=0.5, ambient_k=300.0)
    assert v.energy_balance_ok is False
    assert v.energy_balance_ok is not None
    assert not v.valid


# ---------- P13: Pareto optimization ----------


def test_p13_pareto_front_produced(lib):
    pymoo = pytest.importorskip("pymoo")
    from pyspice_openfoam_agent.optimization.pareto import run_pareto
    from pyspice_openfoam_agent.design.object import Requirements

    req = Requirements(Vin=12, Vout=5, Iout=5, fsw_khz=500, ripple_v=0.05)
    front = run_pareto(lib, req, pop_size=16, n_gen=10, seed=42)
    assert len(front) >= 2
    # all on rank 0 = non-dominated
    assert all(c.rank == 0 for c in front)
    # efficiency/Tj trade-off exists somewhere in the front (physics sanity)
    effs = [c.efficiency for c in front]
    tjs = [c.tj_max_c for c in front]
    assert max(effs) > min(effs) or max(tjs) > min(tjs)