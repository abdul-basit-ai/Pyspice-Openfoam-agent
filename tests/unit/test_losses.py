"""Phase 5 standalone checkpoint tests.

The plan's checkpoint: unit-test the loss functions against a real part's
published efficiency curve class -- back-calculate expected total loss at a
known operating point and compare. The analytical model sits in the same
band as datasheet curves (first-order crossover model; synchronous-only
accounting; ideal-switch simulation), so the end-to-end test checks a
datasheet-plausible efficiency band, while the per-term tests check exact
hand-calculation agreement.

The pure-math tests run anywhere; the end-to-end checkpoint runs where
ngspice does (scripts/dtest.sh).
"""

from __future__ import annotations

import numpy as np
import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.netlist.builder import build_netlist
from pyspice_openfoam_agent.netlist.selector import select_components
from pyspice_openfoam_agent.sizing.engine import Spec, size
from pyspice_openfoam_agent.spice.losses import (
    LossExtractionError,
    _crossover_time,
    _recover_inductor_current,
    extract_losses,
)
from pyspice_openfoam_agent.spice.runner import TransientResult, run_until_steady_state

PUBLISHED = dict(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.05)


def _make_mosfet(part_number: str, Qg: float, Qgd: float, V_plateau: float) -> "MOSFET":
    from pyspice_openfoam_agent.library.schema import MOSFET

    return MOSFET.model_validate(
        dict(
            part_number=part_number, Vds_max=40.0, Rds_on=0.0014, Qg=Qg, Qgd=Qgd,
            V_plateau=V_plateau, Ciss=3900e-12, Id_max=100.0, package="TDSON",
            die_x_mm=5.15, die_y_mm=6.0, die_z_mm=1.0, R_theta_jc=0.8, R_theta_ja=50.0,
            datasheet={"url": "https://example.com"},
        )
    )


def _ngspice_works() -> bool:
    try:
        from PySpice.Spice.NgSpice.Shared import NgSpiceShared

        ng = NgSpiceShared.new_instance()
        ng.load_circuit("* t\nV1 a 0 DC 1\nR1 a 0 1k\n.tran 10n 2u\n.end\n")
        ng.run()
        return True
    except Exception:
        return False


NGSPICE_WORKS = _ngspice_works()
_requires_ngspice = pytest.mark.skipif(not NGSPICE_WORKS, reason="ngspice shared library not usable here")


# ---------- crossover time (the Qgd/V_plateau model) ----------


def test_crossover_time_matches_hand_calc() -> None:
    m = _make_mosfet("X", Qg=49e-9, Qgd=9.5e-9, V_plateau=4.5)
    # I_gate = (5 - 4.5)/2 = 0.25 A; t = 9.5n / 0.25 = 38 ns
    assert abs(_crossover_time(m) - 38e-9) < 1e-9


def test_crossover_time_clamped() -> None:
    # Qgd/Qg must stay inside the schema's [0.05, 0.7] band: Qg=200n keeps it.
    m = _make_mosfet("Y", Qg=200e-9, Qgd=90e-9, V_plateau=4.9)
    # I_gate = (5-4.9)/2 = 0.05 A -> raw 1.8 us, clamped to 200 ns
    assert _crossover_time(m) == 200e-9


# ---------- inductor-current recovery (the ODE integration) ----------


def test_recovery_ripple_slopes_match_physics() -> None:
    """Recovered i_L must show the exact triangular slopes (Vin-Vout)/L
    during HS-ON and -Vout/L during LS-ON; the DC level drifts toward its
    equilibrium with time constant L/DCR, but slopes are exact at all times."""
    Vin, Vout, D, fsw = 12.0, 5.0, 5 / 12, 500e3
    L, dcr = 4.7e-6, 0.0143
    T = 1 / fsw
    pts = 200
    t = np.arange(0, 5 * T, T / pts)
    v_sw = np.where((t % T) < D * T, Vin, 0.0)
    v_out = np.full_like(t, Vout)
    i = _recover_inductor_current(t, v_sw, v_out, L, dcr)

    di = Vout * (1 - D) / (L * fsw)
    i_pp = i.max() - i.min()
    assert abs(i_pp - di) < 0.05 * di, f"ripple pp {i_pp:.3f} vs analytic {di:.3f}"

    slopes = np.diff(i) / (t[1] - t[0])
    on = (t[:-1] % T) < D * T
    on_slopes = slopes[on]
    # strip the 3 samples at each edge of each ON window (transition samples)
    interior = on_slopes[2:-2]
    assert np.allclose(interior, (Vin - Vout) / L, rtol=1e-2)


# ---------- end-to-end extraction on the real buck (ngspice) ----------


@pytest.fixture(scope="module")
def buck_run():
    if not NGSPICE_WORKS:
        pytest.skip("ngspice shared library not usable here")
    lib = load_library()
    spec = Spec(**PUBLISHED)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    run = run_until_steady_state(
        build_netlist(spec, sizing, sel),
        fsw=PUBLISHED["fsw"],
        out_node="out",
        start_cycles=40,
        max_cycles=400,
        expected_level=PUBLISHED["Vout"],
    )
    assert run.steady_state.converged
    lb = extract_losses(
        run.result,
        sel.mosfet,
        sel.inductor.L,
        sel.inductor.DCR,
        vin=PUBLISHED["Vin"],
        vout=PUBLISHED["Vout"],
        fsw=PUBLISHED["fsw"],
        topology=sizing.topology,
        settle_time=run.steady_state.cycle_time,
    )
    return lb, sel


@_requires_ngspice
def test_checkpoint_losses_match_hand_calculations(buck_run) -> None:
    lb, sel = buck_run
    d = lb.losses
    D = PUBLISHED["Vout"] / PUBLISHED["Vin"]
    i2 = lb.i_l_avg**2

    # conduction: I²·R·duty (rel tolerance covers the ripple-shape correction)
    assert d.hs_conduction == pytest.approx(i2 * sel.mosfet.Rds_on * D, rel=0.15)
    assert d.ls_conduction == pytest.approx(i2 * sel.mosfet.Rds_on * (1 - D), rel=0.15)
    assert d.inductor_dcr == pytest.approx(i2 * sel.inductor.DCR, rel=0.15)

    # switching: 0.5*Vin*I*(tr+tf)*fsw, tr=tf=Qgd/I_gate
    t_cross = _crossover_time(sel.mosfet)
    e_sw = 0.5 * PUBLISHED["Vin"] * lb.i_l_avg * (2 * t_cross)
    assert d.hs_switching == pytest.approx(e_sw * PUBLISHED["fsw"], rel=0.15)
    assert d.ls_switching == pytest.approx(e_sw * PUBLISHED["fsw"], rel=0.15)

    # gate drive: 2 * Qg * Vdrv * fsw
    assert d.gate_drive == pytest.approx(2 * sel.mosfet.Qg * 5.0 * PUBLISHED["fsw"], rel=1e-6)


@_requires_ngspice
def test_checkpoint_efficiency_in_datasheet_band(buck_run) -> None:
    """Hard-switched 500kHz 12V/5A sync buck with ~1.4 mOhm FETs and a
    ~14 mOhm-DCR inductor: published designs land in the high-80s/low-90s."""
    lb, _ = buck_run
    p_out = PUBLISHED["Vout"] * PUBLISHED["Iout"]
    eff = p_out / (p_out + lb.losses.total)
    assert 0.80 < eff < 0.97


@_requires_ngspice
def test_per_device_watts_sum_consistently(buck_run) -> None:
    lb, _ = buck_run
    assert sum(lb.per_device_watts.values()) == pytest.approx(lb.losses.total, rel=1e-9)


@_requires_ngspice
def test_checkpoint_current_recovery_sane(buck_run) -> None:
    """i_L stats must match the design: avg ≈ Iout, peak ≈ Iout + di_pp/2."""
    lb, _ = buck_run
    di = PUBLISHED["Vout"] * (1 - PUBLISHED["Vout"] / PUBLISHED["Vin"]) / (4.7e-6 * PUBLISHED["fsw"])
    assert lb.i_l_avg == pytest.approx(PUBLISHED["Iout"], rel=0.15)
    assert lb.i_l_peak == pytest.approx(PUBLISHED["Iout"] + di / 2, rel=0.15)


def test_buck_only_guard() -> None:
    t = np.linspace(0, 1e-3, 100)
    fake = TransientResult(
        vectors={"time": t, "sw": np.zeros(100), "out": np.zeros(100)}
    )
    m = _make_mosfet("Z", Qg=49e-9, Qgd=9.5e-9, V_plateau=4.5)
    with pytest.raises(LossExtractionError, match="buck only"):
        extract_losses(fake, m, 4.7e-6, 0.0143, vin=12, vout=12, fsw=500e3,
                       topology="boost", settle_time=0)
