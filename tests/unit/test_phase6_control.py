"""Tests for Phase 6 control-loop design and the electro-thermal convergence
loop (goals gaps #1 and #2)."""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.design.object import Design, Requirements
from pyspice_openfoam_agent.control_loop.design import (
    ControlDesignError, analyze_control_loop, buck_plant_transfer,
)


# ---------- Phase 6: deterministic control-loop stability ----------


def _buck_design() -> Design:
    d = Design()
    d.requirements = Requirements(Vin=12, Vout=5, Iout=5,
                                  fsw_khz=500, ripple_v=0.05)
    d.topology.name = "buck"
    return d


def test_control_loop_buck_margins_healthy():
    from pyspice_openfoam_agent.sizing.engine import Spec, size

    d = _buck_design()
    spec = Spec(Vin=12, Vout=5, Iout=5, fsw=500e3, ripple_ratio=0.4, Vripple=0.05)
    sizing = size(spec)
    # real selected parts from the Phase 1 library
    lib = __import__("pyspice_openfoam_agent.library.loader",
                     fromlist=["load_library"]).load_library()
    sel = __import__("pyspice_openfoam_agent.netlist.selector",
                     fromlist=["select_components"]).select_components(lib, spec, sizing)
    v = analyze_control_loop(
        d, sizing,
        L=sel.inductor.L,
        C=sel.capacitor.C,
        ESR=sel.capacitor.ESR,
    )
    assert v.passed, f"reasons: {v.reasons}"
    assert v.phase_margin_deg >= 45.0
    assert v.gain_margin_db >= 6.0
    # crossover should be a fraction of f_sw (1/10 target), not past f_sw
    assert 0 < v.crossover_hz < 500e3


def test_control_loop_deterministic():
    """Same design twice => identical margins (no LLM, no RNG)."""
    from pyspice_openfoam_agent.sizing.engine import Spec, size

    d = _buck_design()
    spec = Spec(Vin=12, Vout=5, Iout=5, fsw=500e3, ripple_ratio=0.4, Vripple=0.05)
    sizing = size(spec)
    lib = __import__("pyspice_openfoam_agent.library.loader",
                     fromlist=["load_library"]).load_library()
    sel = __import__("pyspice_openfoam_agent.netlist.selector",
                     fromlist=["select_components"]).select_components(lib, spec, sizing)
    a = analyze_control_loop(d, sizing, sel.inductor.L, sel.capacitor.C, sel.capacitor.ESR)
    b = analyze_control_loop(d, sizing, sel.inductor.L, sel.capacitor.C, sel.capacitor.ESR)
    assert a.phase_margin_deg == b.phase_margin_deg
    assert a.gain_margin_db == b.gain_margin_db


def test_control_loop_grounded_in_real_parts():
    """The margins depend on the SELECTED L/C/ESR, not a nominal guess."""
    from pyspice_openfoam_agent.sizing.engine import Spec, size

    d = _buck_design()
    spec = Spec(Vin=12, Vout=5, Iout=5, fsw=500e3, ripple_ratio=0.4, Vripple=0.05)
    sizing = size(spec)
    # Use a deliberately different (larger) L and ESR and check margins move.
    strict = analyze_control_loop(d, sizing, L=4.7e-6, C=47e-6, ESR=3e-3)
    lax = analyze_control_loop(d, sizing, L=47e-6, C=470e-6, ESR=30e-3)
    # the design pins |T|=1 to f_sw/10, so crossover is identical by
    # construction; the MARGINS must differ with the plant, proving the
    # analysis is grounded in the real component values.
    assert strict.phase_margin_deg != lax.phase_margin_deg
    # and the resonance frequencies are materially different
    assert strict.plant["f_lc_hz"] > lax.plant["f_lc_hz"] * 3


def test_control_loop_plant_rejects_bad_lc():
    with pytest.raises(ControlDesignError):
        buck_plant_transfer(12.0, 0.0, 1e-5, 1e-3, 1.0)


def test_control_loop_boost_not_supported_yet():
    d = _buck_design()
    d.topology.name = "boost"
    from pyspice_openfoam_agent.sizing.engine import Spec, size
    spec = Spec(Vin=5, Vout=12, Iout=5, fsw=500e3, ripple_ratio=0.4, Vripple=0.05)
    sizing = size(spec)
    with pytest.raises(ControlDesignError, match="buck"):
        analyze_control_loop(d, sizing, L=10e-6, C=100e-6, ESR=3e-3)


# ---------- electro-thermal convergence (goals gap #2) ----------


def test_electrothermal_converges():
    from pyspice_openfoam_agent.thermal.electro_thermal import converge

    # stable design: low current, conservative R_theta
    r = converge("TEST1", iout=3.0, fsw_hz=500e3, rds_on_25=6e-3,
                 tempco_ppm=6000, id_max=40, r_theta_ja=50, v_in_m_s=2.0)
    assert r.converged, f"did not converge: {r.reason}"
    assert r.iterations <= 5
    # final Tj must be physically plausible (< 100 C for a light load)
    assert r.final_tj_c < 100.0
    # history is a trace with iter 0 seed + >= 1 iterate
    assert len(r.tj_history) >= 2


def test_electrothermal_detects_divergence():
    """Thermally runaway-ish design fails convergence -> validation failure."""
    from pyspice_openfoam_agent.thermal.electro_thermal import converge

    # absurd: huge R_theta + high current + strong tempco => positive feedback
    r = converge("HOT", iout=50.0, fsw_hz=2e6, rds_on_25=0.1,
                 tempco_ppm=9000, id_max=200, r_theta_ja=150, v_in_m_s=0.1)
    assert not r.converged
    assert "validation failure" in r.reason


def test_electrothermal_respects_tempco():
    """Higher tempco => higher final Tj (positive feedback is physical)."""
    from pyspice_openfoam_agent.thermal.electro_thermal import converge

    base = converge("A", iout=10.0, fsw_hz=500e3, rds_on_25=0.02,
                    tempco_ppm=4000, id_max=60, r_theta_ja=60)
    hot = converge("B", iout=10.0, fsw_hz=500e3, rds_on_25=0.02,
                   tempco_ppm=8000, id_max=60, r_theta_ja=60)
    assert hot.final_tj_c > base.final_tj_c


def test_electrothermal_converge_for_design():
    """End-to-end: wire a real library part through the convergence loop."""
    from pyspice_openfoam_agent.library.loader import load_library
    from pyspice_openfoam_agent.thermal.electro_thermal import converge_for_design

    lib = load_library()
    m = lib.mosfets["BSC014N04LS"]  # low-Rds_on, real thermal data
    req = Requirements(Vin=12, Vout=5, Iout=5, fsw_khz=500, ripple_v=0.05)
    r = converge_for_design(req, m, v_in_m_s=1.5)
    assert r.converged
    assert r.final_tj_c < 150.0