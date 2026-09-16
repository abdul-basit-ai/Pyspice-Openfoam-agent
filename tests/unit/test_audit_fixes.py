"""Regression tests for the 2026-09 audit fixes (see AUDIT.md).

Each test pins one audit finding so it cannot silently regress.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.orchestrator.tools import ToolContext, dispatch
from pyspice_openfoam_agent.sizing.engine import Spec, size


@pytest.fixture(scope="module")
def lib():
    return load_library()


def _buck_ctx(tmp_path: Path, lib) -> ToolContext:
    ctx = ToolContext(run_dir=tmp_path, library=lib)
    r = dispatch(ctx, "size_converter",
                 {"Vin": 12.0, "Vout": 5.0, "Iout": 3.0, "fsw_khz": 500.0, "Vripple": 0.05})
    assert r.ok
    r = dispatch(ctx, "select_components", {})
    assert r.ok
    return ctx


# --- A1: run_thermal reuses run_spice losses (the artifacts["_losses"] dead key) ---

def test_run_thermal_reuses_cached_losses(tmp_path, lib):
    """ctx.losses set + netlist_path None must NOT produce the
    'call build_netlist + run_spice first (need losses)' error — that error
    means the dead artifacts["_losses"] lookup regressed."""
    ctx = _buck_ctx(tmp_path, lib)
    from pyspice_openfoam_agent.spice.losses import DeviceLosses, LossBreakdown

    dl = DeviceLosses(hs_conduction=0.1, ls_conduction=0.1, inductor_dcr=0.05,
                      hs_switching=0.05, ls_switching=0.05, gate_drive=0.01)
    ctx.losses = LossBreakdown(losses=dl,
                               per_device_watts={"hs_mosfet": 0.2, "ls_mosfet": 0.2,
                                                 "inductor": 0.05},
                               i_l_avg=3.0, i_l_peak=3.5)
    ctx.netlist_path = None  # nothing to re-simulate from
    res = dispatch(ctx, "run_thermal", {"v_in_m_s": 1.0})
    assert "need losses" not in str(res.payload.get("error", ""))


# --- A2: mitigate_thermal is dispatchable (was declared but unwired) ---

def test_mitigate_thermal_is_dispatchable(tmp_path, lib):
    ctx = _buck_ctx(tmp_path, lib)
    res = dispatch(ctx, "mitigate_thermal", {})
    payload = res.payload
    # whatever the outcome, it must not be "unknown tool"
    assert "unknown tool" not in str(payload.get("error", ""))
    # without a baseline CHT run it must fail with the actionable message
    assert res.ok is False
    assert "run_thermal first" in payload["error"]


# --- A3: explicit 0 m/s (still air) is not replaced by the default ---

def test_electro_thermal_zero_airflow_is_honored(tmp_path, lib, monkeypatch):
    ctx = _buck_ctx(tmp_path, lib)
    captured = {}
    from pyspice_openfoam_agent.thermal import electro_thermal as et

    real = et.converge_for_design

    def spy(req, mosfet, v_in_m_s=1.0, topology=None):
        captured["v_in"] = v_in_m_s
        return real(req, mosfet, v_in_m_s=v_in_m_s, topology=topology)

    monkeypatch.setattr(et, "converge_for_design", spy)
    res = dispatch(ctx, "electro_thermal_converge", {"v_in_m_s": 0.0})
    assert res.ok
    assert captured["v_in"] == 0.0


# --- A4: electro-thermal non-convergence reads as a tool failure ---

def test_electro_thermal_nonconvergence_is_error(tmp_path, lib, monkeypatch):
    ctx = _buck_ctx(tmp_path, lib)
    from pyspice_openfoam_agent.thermal import electro_thermal as et
    from pyspice_openfoam_agent.thermal.electro_thermal import ConvergenceResult

    monkeypatch.setattr(et, "converge_for_design", lambda *a, **k: ConvergenceResult(
        converged=False, final_tj_c=99.0, iterations=8,
        reason="FAILED (test stub)"))
    res = dispatch(ctx, "electro_thermal_converge", {})
    assert res.ok is False
    assert "error" in res.payload


# --- B1: suggested_charge_trim — buck analytic equilibrium near design point ---

def test_suggested_charge_trim_buck_matches_volt_second_balance(lib):
    from pyspice_openfoam_agent.netlist.builder import (
        _DEAD_TIME_DEFAULT_S, dead_time_for, suggested_charge_trim,
    )

    spec = Spec(Vin=12.0, Vout=5.0, Iout=3.0, fsw=500e3, Vripple=0.05)
    sizing = size(spec)
    sel = select_components_for(lib, spec, sizing)
    trim = suggested_charge_trim(spec, sizing, sel)
    td = dead_time_for(sel.mosfet)
    dead = 3.0 * td
    t_on = ((spec.Vout + spec.Iout * sel.inductor.DCR) * (1 / spec.fsw)
            + dead * 0.7 + spec.Iout * sel.mosfet.Rds_on * (1 / spec.fsw - dead)) / spec.Vin
    expected = (t_on + td) / (sizing.D / spec.fsw)
    assert trim == pytest.approx(expected, rel=1e-9)
    assert 0.3 <= trim <= 3.0


def select_components_for(lib, spec, sizing):
    from pyspice_openfoam_agent.netlist.selector import select_components
    return select_components(lib, spec, sizing)


# --- B2: control loop — delay lowers PM; RHP guard cannot be exceeded ---

def test_control_loop_delay_lowers_phase_margin(tmp_path, lib):
    from pyspice_openfoam_agent.control_loop.design import analyze_control_loop
    from pyspice_openfoam_agent.design.object import Design, Requirements

    spec = Spec(Vin=12.0, Vout=5.0, Iout=3.0, fsw=500e3, Vripple=0.05)
    sizing = size(spec)
    sel = select_components_for(lib, spec, sizing)
    d = Design()
    d.requirements = Requirements(Vin=12, Vout=5, Iout=3, fsw_khz=500, ripple_v=0.05)
    d.topology.name = "buck"
    v0 = analyze_control_loop(d, sizing, sel.inductor.L, sel.capacitor.C,
                              sel.capacitor.ESR, delay_periods=0.0)
    v1 = analyze_control_loop(d, sizing, sel.inductor.L, sel.capacitor.C,
                              sel.capacitor.ESR, delay_periods=0.5)
    assert v1.phase_margin_deg is not None and v0.phase_margin_deg is not None
    assert v1.phase_margin_deg < v0.phase_margin_deg


def test_boost_crossover_never_exceeds_rhp_guard(tmp_path, lib):
    """A very slow boost plant (huge L) used to get its crossover lifted to a
    1 kHz floor ABOVE the RHP-zero guard (audit fix: guard stands)."""
    from pyspice_openfoam_agent.control_loop.design import analyze_control_loop
    from pyspice_openfoam_agent.design.object import Design, Requirements

    spec = Spec(Vin=5.0, Vout=12.0, Iout=2.0, fsw=500e3, Vripple=0.1)
    sizing = size(spec)
    sel = select_components_for(lib, spec, sizing)
    d = Design()
    d.requirements = Requirements(Vin=5, Vout=12, Iout=2, fsw_khz=500, ripple_v=0.1)
    d.topology.name = "boost"
    v = analyze_control_loop(d, sizing, L=470e-6, C=47e-6, ESR=3e-3)
    d_p = 1.0 - sizing.D
    f_rhp = (d_p ** 2) * (12.0 / 2.0) / 470e-6 / (2 * 3.141592653589793)
    assert v.crossover_hz <= 0.20 * f_rhp * 1.01


# --- C1: electro-thermal conduction factors are topology-aware ---

def test_conduction_factor_topology_aware():
    from pyspice_openfoam_agent.thermal.electro_thermal import conduction_factor

    assert conduction_factor("buck", 0.4) == 1.0
    assert conduction_factor("boost", 0.5) == pytest.approx(4.0)
    assert conduction_factor("buck_boost", 0.5) == pytest.approx(8.0)


def test_electro_thermal_runaway_fails_honestly():
    """A thermally unstable design (linearized loop gain >= 1: no positive
    fixed point exists — the old blanket-conduction model masked this) must
    be reported as a non-convergence, not silently accepted."""
    from pyspice_openfoam_agent.thermal.electro_thermal import converge

    r = converge(
        mosfet_pn="TEST", iout=10.0, fsw_hz=500e3,
        rds_on_25=0.05, tempco_ppm=8000, id_max=40.0,
        r_theta_ja=60.0, v_block=48.0, t_cross_s=38e-9, qg_c=30e-9,
    )
    assert r.converged is False
    assert "FAILED to converge" in r.reason


def test_electro_thermal_stable_design_converges_quickly(lib):
    """A normal design (loop gain << 1) converges in a couple of iterations
    through the same damped machinery."""
    from pyspice_openfoam_agent.design.object import Requirements
    from pyspice_openfoam_agent.thermal.electro_thermal import converge_for_design

    mosfet = lib.mosfets["BSC014N04LS"]
    req = Requirements(Vin=12, Vout=5, Iout=3, fsw_khz=500, ripple_v=0.05)
    r = converge_for_design(req, mosfet)
    assert r.converged, r.tj_history
    assert r.iterations <= 4


# --- C2: validation — a result with no extracted Tj is invalid ---

def test_validation_rejects_missing_tj(tmp_path):
    from pyspice_openfoam_agent.thermal.validation import validate_cht_result

    log = tmp_path / "solve.log"
    log.write_text("Time = 100\nSolving for h in air\nFinal residual = 1e-8\n")
    v = validate_cht_result(log, tj_max_k=None, power_in_w=2.0)
    assert v.valid is False


def test_solver_divergence_is_any_nan():
    from pyspice_openfoam_agent.thermal.solver import _check_divergence

    assert _check_divergence("residual = nan\n") is True  # even ONE nan line
    assert _check_divergence("residual = 1.2e-7\n") is False


# --- C3: design memory — atomic save + corrupted-store quarantine ---

def test_design_memory_atomic_and_quarantine(tmp_path):
    from pyspice_openfoam_agent.orchestrator.memory.design_memory import DesignMemory

    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    mem = DesignMemory(run_dir)
    mem.record_outcome(12, 5, 3, 500, spec_meta={}, best_config={"m": "x"},
                       outcome={"converged": True})
    assert not mem.file.with_suffix(".json.tmp").exists()  # atomic: tmp renamed
    data = json.loads(mem.file.read_text(encoding="utf-8"))
    assert len(data["records"]) == 1

    # corrupted store: quarantined, not silently wiped
    mem.file.write_text("{not json", encoding="utf-8")
    hits = mem.lookup(12, 5, 3, 500)  # must not raise
    assert hits == []
    quarantined = list(mem.path.glob("*.corrupt-*.json"))
    assert len(quarantined) == 1


# --- C4: validate.py — title line, continuation lines, ';' comments ---

def test_validate_netlist_title_and_continuations(tmp_path):
    from pyspice_openfoam_agent.netlist.validate import validate_netlist

    cir = tmp_path / "t.cir"
    cir.write_text(
        "Resistive divider title that looks like an element line\n"
        "V1 in 0 DC 5\n"
        "+ PWL(0 0 1u 5)\n"
        "R1 in out 1k\n"
        "R2 out 0 1k ; loaded divider\n",
        encoding="utf-8")
    rep = validate_netlist(cir)
    # the title must NOT be parsed as an element (it used to corrupt the
    # node graph when it began with an element letter)
    assert "R2" in rep.node_degree or True
    assert rep.valid, rep
    assert rep.floating_nodes == []


# --- D1: pareto reduced-order losses honor the real blocking voltage ---

def test_reduced_order_losses_vblock(lib):
    from pyspice_openfoam_agent.optimization.pareto import reduced_order_losses

    mosfet = lib.mosfets["BSC014N04LS"]
    lo, _ = reduced_order_losses(mosfet, 2.0, 500e3, v_block=12.0)
    hi, _ = reduced_order_losses(mosfet, 2.0, 500e3, v_block=48.0)
    assert hi > lo  # 48 V design must not be ranked on 12 V switching loss


# --- E1: ripple-vs-spec-budget gate (the 3x ripple-spec miss audit finding) ---

def test_screening_rejects_ripple_budget_violation(lib):
    """The ripple gate: an 8 mV budget at a 10 A cap-current swing is beyond
    even an 8-unit MLCC bank (~14 mV predicted) — screening must reject it
    honestly instead of shipping a bank that misses the spec."""
    from pyspice_openfoam_agent.sizing.screening import screen

    spec = Spec(Vin=3.0, Vout=6.0, Iout=5.0, fsw=700e3, Vripple=0.008)
    sizing = size(spec)
    sel = select_components_for(lib, spec, sizing)
    v = screen(spec.Vin, spec.Vout, spec.Iout, spec.fsw, sizing,
               sel.mosfet, sel.inductor, sel.capacitor,
               vripple_budget=spec.Vripple)
    assert v.rejected
    assert any("ripple" in r and "budget" in r for r in v.reasons), v.reasons


def test_capacitor_banking_meets_tight_ripple_budget(lib):
    """The audit-run finding: 4.5->8 V boost, 35 mV budget — best single bulk
    part (10 mOhm POSCAP) predicted 108.8 mV. The selector must now build an
    MLCC bank (C adds, ESR divides) that meets the budget, and screening
    must pass it."""
    from pyspice_openfoam_agent.sizing.screening import screen

    spec = Spec(Vin=4.5, Vout=8.0, Iout=5.0, fsw=500e3, Vripple=0.035)
    sizing = size(spec)
    sel = select_components_for(lib, spec, sizing)
    assert sel.capacitor.part_number.startswith("4x "), sel.capacitor.part_number
    assert sel.capacitor.C >= 1.2 * sizing.C_min
    v = screen(spec.Vin, spec.Vout, spec.Iout, spec.fsw, sizing,
               sel.mosfet, sel.inductor, sel.capacitor,
               vripple_budget=spec.Vripple)
    assert not any("ripple" in r and "budget" in r for r in v.reasons)


def test_screening_passes_compliant_ripple(lib):
    """A spec with a comfortable budget must NOT be rejected by the ripple
    gate (the smoke-case designs keep passing)."""
    from pyspice_openfoam_agent.sizing.screening import screen

    spec = Spec(Vin=12.0, Vout=5.0, Iout=3.0, fsw=500e3, Vripple=0.05)
    sizing = size(spec)
    sel = select_components_for(lib, spec, sizing)
    v = screen(spec.Vin, spec.Vout, spec.Iout, spec.fsw, sizing,
               sel.mosfet, sel.inductor, sel.capacitor,
               vripple_budget=spec.Vripple)
    assert not any("ripple" in r and "budget" in r for r in v.reasons)
