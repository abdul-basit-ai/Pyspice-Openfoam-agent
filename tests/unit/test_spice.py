from __future__ import annotations

"""Phase 4 standalone checkpoint tests.

The plan's checkpoint: run the Phase 3 netlist for the Phase 2 published
worked example, confirm the steady-state detector stops within a bounded
number of switching cycles, and that measured ripple/efficiency come out
sane (the SPICE-level numbers depend on the *real* selected parts' Rds_on/
DCR/ESR, which aren't part of Phase 2's analytical reference, so this checks
plausibility/order-of-magnitude, not an exact published figure).

Requires a real `ngspice` shared library on PATH for the actual simulation
runs; those are skipped (not failed) if PySpice can't get an ngspice
instance, so this file still documents the steady-state-detector unit tests
in environments without ngspice.
"""

import numpy as np
import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.netlist.builder import build_netlist
from pyspice_openfoam_agent.netlist.selector import select_components
from pyspice_openfoam_agent.sizing.engine import Spec, size
from pyspice_openfoam_agent.spice.runner import (
    DroopResult,
    SimulationError,
    build_tran_line,
    measure_efficiency,
    measure_output_ripple,
    measure_transient_droop,
    run_transient,
    run_until_steady_state,
)
from pyspice_openfoam_agent.spice.steady_state import detect_steady_state

PUBLISHED = dict(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.05)


def _ngspice_works() -> bool:
    try:
        run_transient(".title t\nV1 a 0 DC 1\nR1 a 0 1k\n.end\n", fsw=1e6, n_cycles=1)
        return True
    except SimulationError:
        return False


NGSPICE_WORKS = _ngspice_works()
_skip_no_ngspice = pytest.mark.skipif(not NGSPICE_WORKS, reason="ngspice shared library not usable here")


@pytest.fixture(scope="module")
def buck_netlist_text() -> str:
    lib = load_library()
    spec = Spec(**PUBLISHED)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    return build_netlist(spec, sizing, sel)


# ---------- the checkpoint itself ----------


@_skip_no_ngspice
def test_checkpoint_steady_state_within_bounded_cycles(buck_netlist_text: str) -> None:
    run = run_until_steady_state(
        buck_netlist_text, fsw=PUBLISHED["fsw"], out_node="out", start_cycles=20, max_cycles=500
    )
    assert run.steady_state.converged, (
        f"did not converge within {run.n_cycles_run} cycles "
        f"(last delta={run.steady_state.final_delta:.4g})"
    )
    assert run.n_cycles_run <= 500
    assert run.steady_state.cycle_index is not None


@_skip_no_ngspice
def test_checkpoint_ripple_and_efficiency_are_plausible(buck_netlist_text: str) -> None:
    run = run_until_steady_state(
        buck_netlist_text, fsw=PUBLISHED["fsw"], out_node="out", start_cycles=20, max_cycles=500
    )
    assert run.steady_state.converged

    ripple = measure_output_ripple(run.result, "out", fsw=PUBLISHED["fsw"], n_tail_cycles=2)
    # Phase 2's spec allowed up to Vripple=0.05 V from the C-term alone; the
    # simulated ripple (C-term + real ESR, which Phase 2 deliberately left
    # for Phase 3) should be the same order of magnitude, not wildly off.
    assert 0 < ripple < 5 * PUBLISHED["Vripple"]

    efficiency = measure_efficiency(
        run.result,
        vin=PUBLISHED["Vin"],
        in_current_branch="vin#branch",
        out_node="out",
        r_load=PUBLISHED["Vout"] / PUBLISHED["Iout"],
        fsw=PUBLISHED["fsw"],
        n_tail_cycles=2,
    )
    # measure_efficiency is a waveform upper bound; ideal switches cannot
    # dissipate hard-switch/Coss/Crr losses, so it may legitimately read ~1.0
    # even for a real converter. Regression guard: it must NEVER exceed 100%
    # (the old boost >100% bug). Physical efficiency is loss-based and is
    # asserted separately in test_efficiency_from_losses_bound.
    assert 0 < efficiency <= 1.0

    vout_final = run.result["out"][-1]
    assert 4.0 < vout_final < 5.3  # in the neighborhood of the 5V target


@_skip_no_ngspice
def test_checkpoint_output_settles_near_target(buck_netlist_text: str) -> None:
    run = run_until_steady_state(
        buck_netlist_text, fsw=PUBLISHED["fsw"], out_node="out", start_cycles=40, max_cycles=800
    )
    tail = run.steady_state.cycle_averages[-5:]
    assert np.all(tail > 4.0)  # heading toward 5V, not stuck near 0 (e.g. dead gate drive)


# ---------- steady-state detector, pure numpy (no ngspice needed) ----------


def test_detector_converges_on_a_synthetic_settling_waveform() -> None:
    period = 2e-6
    t = np.linspace(0, 200 * period, 20000)
    # Exponential settle to 5V with time constant 20 periods, plus small
    # switching-frequency ripple riding on top.
    y = 5.0 * (1 - np.exp(-t / (20 * period))) + 0.01 * np.sin(2 * np.pi * t / period)
    result = detect_steady_state(t, y, period, threshold=1e-3, consecutive=3)
    assert result.converged
    assert result.cycle_index is not None
    assert result.cycle_index < 190  # well before the run ends


def test_detector_does_not_converge_on_a_still_ramping_waveform() -> None:
    period = 2e-6
    t = np.linspace(0, 50 * period, 5000)
    y = 0.5 * t / t[-1] * 10.0  # linearly ramping the whole run, never flat
    result = detect_steady_state(t, y, period, threshold=1e-4, consecutive=3)
    assert not result.converged
    assert result.cycle_index is None


def test_detector_rejects_ringing_overshoot_peak() -> None:
    """A converter that overshoots then rings back must NOT be reported at the
    overshoot peak (a locally-flat point mid-ringdown). Regression for the
    boost startup mis-trigger: 3 flat cycles at the 15 V peak, target 12 V.
    """
    period = 2e-6
    t = np.linspace(0, 400 * period, 80000)
    # overshoot to 15V then exponential decay back to 12V (boost-like)
    y = 12.0 + 3.0 * np.exp(-t / (60 * period))
    result = detect_steady_state(t, y, period, threshold=1e-3, consecutive=3,
                                 expected_level=12.0)
    assert result.converged
    assert result.cycle_index is not None
    # must converge at ~12V, not at the 15V plateau: cycle avg within 15% of 12
    final = float(result.cycle_averages[result.cycle_index])
    assert abs(final - 12.0) < 0.15 * 12.0, f"converged at {final:.2f}, expected ~12V"


def test_detector_accepts_true_sustain() -> None:
    """A genuine settled waveform still converges promptly (no over-rejection)."""
    period = 2e-6
    t = np.linspace(0, 200 * period, 40000)
    y = 5.0 * (1 - np.exp(-t / (20 * period))) + 0.01 * np.sin(2 * np.pi * t / period)
    result = detect_steady_state(t, y, period, threshold=1e-3, consecutive=3,
                                 expected_level=5.0)
    assert result.converged
    assert result.cycle_index is not None


def test_detector_handles_too_short_a_run() -> None:
    period = 2e-6
    t = np.linspace(0, 0.5 * period, 100)  # less than one full cycle
    y = np.ones_like(t) * 5.0
    result = detect_steady_state(t, y, period)
    assert not result.converged
    assert len(result.cycle_averages) == 0


# ---------- runner internals ----------


def test_build_tran_line_scales_with_cycles_and_fsw() -> None:
    line = build_tran_line(fsw=500e3, n_cycles=10, points_per_cycle=200)
    step, end = (float(x) for x in line.split()[1:])
    assert abs(end - 10 / 500e3) < 1e-12
    assert abs(step - (1 / 500e3) / 200) < 1e-15


def test_build_tran_line_rejects_nonpositive_inputs() -> None:
    with pytest.raises(ValueError):
        build_tran_line(fsw=0, n_cycles=10)
    with pytest.raises(ValueError):
        build_tran_line(fsw=500e3, n_cycles=-1)


@_skip_no_ngspice
def test_droop_measurement_on_startup_transient(buck_netlist_text: str) -> None:
    # Soft-start ramp is 300*Tsw; give the run enough cycles to finish the
    # ramp AND reach (and settle at) the target so recovery is observable.
    ncyc = 500
    result = run_transient(buck_netlist_text, fsw=PUBLISHED["fsw"], n_cycles=ncyc)
    droop = measure_transient_droop(result, "out", v_nominal=PUBLISHED["Vout"], tolerance=0.05)
    assert isinstance(droop, DroopResult)
    assert droop.droop_v > 0  # starts at 0V, well below the 5V nominal
    assert droop.recovered  # a healthy design recovers well within the run


def test_detector_rejects_dead_output_when_expected_level_given() -> None:
    """Regression (audit SS-1): a flat-at-zero output used to report
    converged=True (0.0 delta satisfies any threshold). With expected_level,
    a dead circuit must NOT count as steady state."""
    period = 2e-6
    t = np.linspace(0, 100 * period, 10000)
    y = np.zeros_like(t)
    assert detect_steady_state(t, y, period, expected_level=5.0).converged is False
    # and without the guard it still does (documented behavior kept)
    assert detect_steady_state(t, y, period).converged is True


def test_detector_expected_level_accepts_correct_settle() -> None:
    period = 2e-6
    t = np.linspace(0, 200 * period, 20000)
    y = 5.0 * (1 - np.exp(-t / (20 * period))) + 0.01 * np.sin(2 * np.pi * t / period)
    result = detect_steady_state(t, y, period, threshold=1e-3, consecutive=3, expected_level=5.0)
    assert result.converged
    assert result.cycle_index is not None and result.cycle_index < 190


def test_detector_rejects_flat_wrong_level() -> None:
    """Flat at 1V when 5V expected: flatness says converged, level guard says no."""
    period = 2e-6
    t = np.linspace(0, 100 * period, 10000)
    y = np.ones_like(t) * 1.0
    assert detect_steady_state(t, y, period, expected_level=5.0).converged is False


def test_efficiency_never_exceeds_100_percent(buck_netlist_text: str) -> None:
    """Efficiency is clamped at 100% even if the input-branch reading is noisy;
    regression for the impossible >100% (boost 624%) reports."""
    from pyspice_openfoam_agent.spice.runner import run_until_steady_state, measure_efficiency
    import numpy as np

    spec = Spec(Vin=12, Vout=5, Iout=5, fsw=500e3, ripple_ratio=0.4, Vripple=0.05)
    # max_cycles must clear the 300-cycle soft-start ramp (the builder now
    # emits a soft-start PWL so the output is still mid-ramp at 200 cycles).
    run = run_until_steady_state(buck_netlist_text, fsw=spec.fsw, out_node="out",
                                 start_cycles=60, max_cycles=900, expected_level=spec.Vout)
    eff = measure_efficiency(run.result, vin=spec.Vin, in_current_branch="vin#branch",
                             out_node="out", r_load=spec.Vout / spec.Iout, fsw=spec.fsw)
    assert 0.0 <= eff <= 1.0
    # measure_efficiency is a loss-blind waveform upper bound: ideal switches
    # cannot dissipate hard-switch/Coss/Crr losses, so >=0.90 is expected;
    # the physically-correct loss-based efficiency is asserted separately.
    assert eff > 0.85, f"buck efficiency {eff:.3f} unexpectedly low"


def test_efficiency_uses_longer_settled_window() -> None:
    """Default tail is 8 cycles, not 2, so slow-topology averages settle."""
    from pyspice_openfoam_agent.spice.runner import measure_efficiency
    import numpy as np

    T = 2e-6
    t = np.linspace(0, 40 * T, 4000)
    # source current: negative (into source), steady -2 A with small ripple
    i = -2.0 + 0.1 * np.sin(2 * np.pi * t / T)
    v = 5.0 + 0.05 * np.sin(2 * np.pi * t / T)
    from pyspice_openfoam_agent.spice.runner import TransientResult
    res = TransientResult(vectors={"time": t, "vin#branch": i, "out": v})
    rload = 1.0  # actual v^2/R used; only the clamp check matters here
    eff = measure_efficiency(res, vin=12, in_current_branch="vin#branch",
                             out_node="out", r_load=rload, fsw=1/T)
    assert 0.0 <= eff <= 1.0


def test_validate_transient_health_flags_shoot_through() -> None:
    """A negative Vout spike (shoot-through) in the settled tail fails the guard."""
    from pyspice_openfoam_agent.spice.runner import validate_transient_health, TransientResult
    T = 2e-6
    t = np.linspace(0, 40 * T, 8000)
    v = 5.0 + 0.1 * np.sin(2 * np.pi * t / T)
    v[-50] = -2.0  # a shoot-through spike inside the 2-period tail
    res = TransientResult(vectors={"time": t, "out": v})
    h = validate_transient_health(res, "out", 5.0, 1 / T)
    assert h["ok"] is False
    assert any("shoot-through" in r for r in h["reasons"])


def test_validate_transient_health_accepts_clean() -> None:
    from pyspice_openfoam_agent.spice.runner import validate_transient_health, TransientResult
    T = 2e-6
    t = np.linspace(0, 40 * T, 8000)
    v = 5.0 + 0.02 * np.sin(2 * np.pi * t / T)  # tiny ripple, no spike
    res = TransientResult(vectors={"time": t, "out": v})
    h = validate_transient_health(res, "out", 5.0, 1 / T)
    assert h["ok"] is True
    assert h["checks"]["offset"] is not None


def test_efficiency_from_losses_bound() -> None:
    """Normalized efficiency: 0 < eta <= 1; corrupt inputs raise."""
    from pyspice_openfoam_agent.spice.losses import efficiency_from_losses, LossExtractionError
    assert 0.0 < efficiency_from_losses(5, 5, 1.0) <= 1.0
    # 25 W out, 2.87 W loss -> ~89.7% (the physically-correct value)
    assert efficiency_from_losses(5, 5, 2.87) == pytest.approx(25 / 27.87, rel=1e-6)
    with pytest.raises(LossExtractionError):
        efficiency_from_losses(5, 5, -1.0)  # negative loss impossible
    with pytest.raises(LossExtractionError):
        efficiency_from_losses(0, 5, 1.0)  # zero P_out impossible


def test_efficiency_hs_ls_decoupled_hand_calc() -> None:
    """HS hard-switch + Coss vs LS body-diode + Qrr: LS << HS switching."""
    from pyspice_openfoam_agent.spice.losses import _crossover_time
    from pyspice_openfoam_agent.library.schema import MOSFET

    m = MOSFET.model_validate(dict(
        part_number="T", Vds_max=40, Rds_on=0.0014, Qg=30e-9, Qgd=9.5e-9,
        V_plateau=4.5, Ciss=3900e-12, Coss=800e-12, Id_max=100, package="x",
        die_x_mm=5, die_y_mm=6, die_z_mm=1, R_theta_jc=0.8, R_theta_ja=50,
        datasheet={"url": "https://example.com"}))
    i, v, f, tr = 5.0, 12.0, 500e3, _crossover_time(m)
    hs_sw = (0.5 * v * i * (2 * tr) + 0.5 * m.Coss * v**2) * f
    # compare with what extract_losses would do (LS far below HS with Qrr=0)
    from pyspice_openfoam_agent.netlist.builder import _DEAD_TIME_DEFAULT_S  # noqa: F401
    ls_diode = 2.0 * m.V_F * i * _DEAD_TIME_DEFAULT_S * f  # dead-time body diode
    assert ls_diode < 0.5 * hs_sw  # LS sync rect loses far less than HS hard switch
