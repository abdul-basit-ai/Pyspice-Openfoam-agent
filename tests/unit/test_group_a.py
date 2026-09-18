"""Group A additions (remain_plan.md): Phase 1 IC data + Phase 5 IC selection,
Phase 6/8 step tests, Phase 8 sweeps, Phase 9 cap-ESR loss, Phase 14 Pareto
tool. Pure-logic tests here; the real-ngspice end-to-end is gated like
test_spice.py.
"""

from __future__ import annotations

import numpy as np
import pytest

from pyspice_openfoam_agent.library.loader import (
    load_library,
    query_controllers,
    query_diodes,
    query_gate_drivers,
)
from pyspice_openfoam_agent.netlist.selector import (
    select_components,
    select_gate_driver,
)
from pyspice_openfoam_agent.sizing.engine import Spec, size
from pyspice_openfoam_agent.spice.losses import DeviceLosses
from pyspice_openfoam_agent.spice.runner import TransientResult
from pyspice_openfoam_agent.spice.step_tests import (
    StepTestError,
    inject_input_step,
    inject_load_step,
)
from pyspice_openfoam_agent.spice.sweeps import SweepError, replace_load


@pytest.fixture(scope="module")
def lib():
    return load_library()


# ---------------- A1: Phase 1 IC data ----------------

def test_library_counts_include_ic_categories(lib):
    counts = lib.counts()
    assert counts["gate_drivers"] >= 3
    assert counts["controllers"] >= 4
    assert counts["diodes"] >= 5


def test_gate_driver_queries(lib):
    # rail window filter: the 5 V behavioral rail
    low = query_gate_drivers(lib, v_drive_min=5.0, v_drive_max=5.0, half_bridge=False)
    assert {g.part_number for g in low} >= {"UCC27517DBV", "TC4420EOA"}
    # half-bridge filter (sync buck)
    hb = query_gate_drivers(lib, half_bridge=True)
    assert [g.part_number for g in hb] == ["LM5107MAX"]
    # peak-current floor prunes weak drivers
    strong = query_gate_drivers(lib, v_drive_min=5.0, v_drive_max=5.0,
                                peak_source_min=5.0, half_bridge=False)
    assert all(g.peak_source_a >= 5.0 for g in strong)
    assert "UCC27517DBV" not in {g.part_number for g in strong}


def test_controller_queries_cover_fsw_windows(lib):
    vm_500k = query_controllers(lib, control_law="voltage", fsw_hz=500e3)
    names = {c.part_number for c in vm_500k}
    assert "LM27402MHX" in names and "TPS40057PWPR" in names
    assert "SG3525A" not in names and "TL494" not in names  # 400k/300k caps
    vm_300k = query_controllers(lib, control_law="voltage", fsw_hz=300e3)
    assert "SG3525A" in {c.part_number for c in vm_300k}


def test_diode_query_sorted_by_vf(lib):
    d = query_diodes(lib, Vr_min=36.0, I_avg_min=2.0)
    vfs = [x.Vf_25 for x in d]
    assert vfs == sorted(vfs)
    assert all(x.Vr_max >= 36.0 and x.I_avg_max >= 2.0 for x in d)


# ---------------- A2: Phase 5 IC selection ----------------

def test_buck_selects_half_bridge_driver_and_controller(lib):
    spec = Spec(Vin=12, Vout=5, Iout=2, fsw=500e3, Vripple=0.05)
    sel = select_components(lib, spec, size(spec))
    assert sel.gate_driver is not None
    assert sel.gate_driver.part_number == "LM5107MAX"
    assert sel.controller is not None
    # any note mentioning the VDD rail mismatch documents the aux-rail need
    assert any("VDD rail" in n for n in sel.notes)


def test_boost_500khz_controller_gap_reported_honestly(lib):
    spec = Spec(Vin=5, Vout=12, Iout=2, fsw=500e3, Vripple=0.1)
    sel = select_components(lib, spec, size(spec))
    # low-side driver for the ground-referenced switches
    assert sel.gate_driver is not None
    assert sel.gate_driver.part_number in {"UCC27517DBV", "TC4420EOA"}
    # no generic voltage-mode controller covers 500 kHz -> None + note
    assert sel.controller is None
    assert any("no voltage-mode library controller covers" in n for n in sel.notes)


def test_boost_300khz_gets_generic_controller(lib):
    spec = Spec(Vin=5, Vout=12, Iout=2, fsw=300e3, Vripple=0.1)
    sel = select_components(lib, spec, size(spec))
    assert sel.controller is not None
    assert sel.controller.part_number == "SG3525A"


# ---------------- A6: Phase 9 cap-ESR loss ----------------

def test_device_losses_total_includes_capacitor_esr():
    dl = DeviceLosses(hs_conduction=1.0, ls_conduction=1.0, inductor_dcr=0.5,
                      hs_switching=0.25, ls_switching=0.25, gate_drive=0.1,
                      capacitor_esr=0.05)
    assert dl.total == pytest.approx(3.15)
    dl0 = DeviceLosses(hs_conduction=1.0, ls_conduction=1.0, inductor_dcr=0.5,
                       hs_switching=0.25, ls_switching=0.25, gate_drive=0.1)
    assert dl0.total == pytest.approx(3.10)


def test_extract_losses_cap_esr_computed(monkeypatch):
    """Synthetic buck steady state: i_L = 2.0 +/- 0.2 (triangle), so the cap
    current is the +/-0.2 A ripple -> ESR loss is ESR * rms(i_L - I_out)^2,
    computable by hand (I_out ~ 2.0 A from v_out/R_load)."""
    import pyspice_openfoam_agent.spice.losses as L

    fsw = 100e3
    tsw = 1.0 / fsw
    # steady window only: 20 cycles, 200 samples/cycle
    n = 4000
    t = 1e-3 + np.arange(n) * (20 * tsw / n)
    # buck switch node: 12 V for D*Tsw, ~0 for the rest (D = 0.45)
    phase = (t % tsw) / tsw
    v_sw = np.where(phase < 0.45, 12.0, 0.02)
    v_out = np.full_like(t, 5.0)
    res = TransientResult(vectors={
        "time": t, "sw": v_sw, "out": v_out, "in0": np.full_like(t, 12.0),
    })
    # inductor current: 2.0 A DC + 0.4 A pp smooth ripple at fsw (shape is
    # irrelevant — the expected loss is computed from this same array)
    i_l = 2.0 + 0.2 * np.cos(2.0 * np.pi * phase)
    monkeypatch.setattr(L, "_recover_inductor_current",
                        lambda *a, **k: i_l.copy())

    class FakeMos:
        Rds_on = 0.004
        Qgd = 5e-9
        Qg = 25e-9
        V_plateau = 3.0
        Coss = None
        Qrr = None
        V_F = 0.7

    lb = L.extract_losses(
        res, FakeMos(), inductor_L=4.7e-6, inductor_dcr=0.014,
        vin=12.0, vout=5.0, fsw=fsw, topology="buck", settle_time=1e-3,
        cap_esr=0.010, iout=2.0,
    )
    i_cap = i_l - v_out * (2.0 / 5.0)  # i_feed - i_out for a buck
    expected = float(np.mean(i_cap**2)) * 0.010
    assert lb.losses.capacitor_esr == pytest.approx(expected, rel=1e-6)
    assert any("ESR loss" in n for n in lb.notes)
    # without cap data the term is 0 and total shrinks accordingly
    lb0 = L.extract_losses(
        res, FakeMos(), inductor_L=4.7e-6, inductor_dcr=0.014,
        vin=12.0, vout=5.0, fsw=fsw, topology="buck", settle_time=1e-3,
    )
    assert lb0.losses.capacitor_esr == 0.0


# ---------------- A3: step-test injection ----------------

_NL = ("Buck test\n"
       "Vin in 0 DC 12 PWL(0 0 0.6m 12)\n"
       "Rload out 0 2.5\n"
       ".tran 1u\n"
       ".end\n")


def test_inject_load_step_format_and_idempotency():
    out = inject_load_step(_NL, 1.0, 2e-3)
    assert "Istep out 0 PWL(0 0 0.002 0 0.002000002 1)" in out
    assert out.index("Istep") < out.index(".end")
    # re-injecting replaces the previous step, never stacks
    out2 = inject_load_step(out, 2.0, 3e-3)
    assert out2.count("Istep") == 1
    assert "0.003000002 2" in out2


def test_inject_input_step_extends_soft_start_pwl():
    out = inject_input_step(_NL, 0.1, 2e-3)
    assert "PWL(0 0 0.6m 12 0.002 12 0.002000002 13.2)" in out
    with pytest.raises(StepTestError):
        inject_input_step("No source here\n.end\n", 0.1, 1e-3)


def test_inject_load_step_without_end_appends():
    out = inject_load_step("Rload out 0 2.5\n", 1.0, 1e-3)
    assert "Istep" in out


# ---------------- A4: load-sweep surgery ----------------

def test_replace_load_value():
    out = replace_load(_NL, 10.0)
    assert "Rload out 0 10" in out
    assert "2.5" not in out.split("Rload")[1]
    with pytest.raises(SweepError):
        replace_load("no load line\n", 1.0)


# ---------------- tool wiring (no ngspice needed) ----------------

def _ctx(tmp_path, lib):
    from pyspice_openfoam_agent.orchestrator.tools import ToolContext

    return ToolContext(run_dir=tmp_path, library=lib)


def test_step_tests_tool_requires_spice_first(tmp_path, lib):
    from pyspice_openfoam_agent.orchestrator.tools import dispatch

    r = dispatch(_ctx(tmp_path, lib), "run_step_tests", {})
    assert not r.ok
    assert "run_spice first" in r.payload["error"]


def test_sweeps_tool_requires_netlist(tmp_path, lib):
    from pyspice_openfoam_agent.orchestrator.tools import dispatch

    r = dispatch(_ctx(tmp_path, lib), "run_sweeps", {"mode": "load"})
    assert not r.ok


def test_pareto_tool_requires_spec(tmp_path, lib):
    from pyspice_openfoam_agent.orchestrator.tools import dispatch

    r = dispatch(_ctx(tmp_path, lib), "optimize_pareto", {})
    assert not r.ok
    assert "size_converter first" in r.payload["error"]


def test_new_tools_in_schema_and_dispatch():
    from pyspice_openfoam_agent.orchestrator.graph import SYSTEM_PROMPT
    from pyspice_openfoam_agent.orchestrator.tools import TOOL_SCHEMAS

    names = {t["name"] for t in TOOL_SCHEMAS}
    assert {"run_step_tests", "run_sweeps", "optimize_pareto"} <= names
    for n in ("run_step_tests", "run_sweeps", "optimize_pareto"):
        assert n in SYSTEM_PROMPT


# ---------------- A5: Pareto + crossover guard ----------------

def test_crossover_time_zero_gate_current_guard():
    """V_plateau at the 5 V drive rail => zero gate current => the shared
    crossover helper must clamp, not divide by zero (surfaced by the Pareto
    tool on BSC060N10NS3, V_plateau=5.0)."""
    from pyspice_openfoam_agent.spice.losses import crossover_time

    class P:
        Qgd = 9e-9
        V_plateau = 5.0

    assert crossover_time(P()) == pytest.approx(200e-9)  # clamped high


def test_reduced_order_losses_sane_for_plateau_at_rail():
    from pyspice_openfoam_agent.library.schema import MOSFET, DatasheetRef
    from pyspice_openfoam_agent.optimization.pareto import reduced_order_losses

    m = MOSFET(
        part_number="TEST-5V0-PLATEAU", Vds_max=100.0, Rds_on=0.006,
        Qg=44e-9, Qgd=9e-9, V_plateau=5.0, Ciss=3200e-12,
        Id_max=55.0, package="PG-TDSON-8", die_x_mm=5.15, die_y_mm=6.0,
        die_z_mm=1.0, R_theta_jc=1.0, R_theta_ja=50.0,
        datasheet=DatasheetRef(url="https://example.com"),
    )
    loss, _ = reduced_order_losses(m, iout=2.0, fsw_hz=360e3, v_block=12.0)
    assert 0.0 < loss < 5.0  # watts, not 1e8


def test_run_pareto_front_is_physical():
    from pyspice_openfoam_agent.design.object import Requirements
    from pyspice_openfoam_agent.optimization.pareto import run_pareto

    req = Requirements(Vin=12.0, Vout=5.0, Iout=2.0, fsw_khz=500.0,
                       ripple_v=0.05)
    front = run_pareto(load_library(), req, pop_size=12, n_gen=4)
    assert front, "front must not be empty for a satisfiable spec"
    for c in front:
        assert 0.0 < c.efficiency < 1.0
        assert 20.0 < c.tj_max_c < 200.0  # degC, never 1e9 garbage


# ---------------- real-ngspice end-to-end (gated like test_spice.py) ----------------

def _ngspice_works():
    try:
        from pyspice_openfoam_agent.spice.runner import run_transient

        run_transient("* smoke\nV1 a 0 DC 1\nR1 a 0 1k\n.tran 1u 2u\n.end\n",
                      fsw=1e6, n_cycles=2, points_per_cycle=10)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ngspice_works(), reason="ngspice unavailable")
def test_step_tests_end_to_end_buck(tmp_path, lib):
    """Real design, real servo, real step injection + measurement."""
    from pyspice_openfoam_agent.orchestrator.tools import dispatch

    ctx = _ctx(tmp_path, lib)
    for tool, args in [
        ("size_converter", dict(Vin=12, Vout=5, Iout=2, fsw_khz=500, Vripple=0.05)),
        ("select_components", {}), ("build_netlist", {}), ("run_spice", {}),
    ]:
        r = dispatch(ctx, tool, args)
        assert r.ok, (tool, r.payload)
    r = dispatch(ctx, "run_step_tests", dict(step_fraction=0.5, input_step_frac=0.1))
    assert r.ok, r.payload
    ls = r.payload["load_step"]
    assert ls["v_before"] == pytest.approx(5.0, abs=0.3)
    assert ls["undershoot_V"] > 0
    # open-loop: a +10% input step moves Vout ~ +10% at fixed duty
    iv = r.payload["input_step"]
    assert iv["overshoot_V"] > 0.2

