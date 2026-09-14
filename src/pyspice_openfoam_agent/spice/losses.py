"""Phase 5: per-device loss extraction for the thermal stage.

Loss accounting for the synchronous converter topologies Phase 3 emits
(HS + LS MOSFET pair modeled as ideal switches with the real part's Ron,
real DCR/ESR parasitics). Per-device, per steady-state period (user decision:
synchronous-only, no body-diode term):

  P_hs_cond  = <i_L² * Ron> while the HS switch conducts
  P_ls_cond  = <i_L² * Ron> while the LS switch is ON
  P_l_dcr    = <i_L² * DCR> (continuous)
  P_sw_hs    = ½ * Vblock * I * (t_r + t_f) * fsw   (crossover model)
  P_sw_ls    = same model, LS's commutation event
  P_gate     = Qg * V_driver * fsw per driven switch

Why switching loss is a model, not a waveform integral: the Phase 3 netlist
models each MOSFET as an ideal voltage-controlled switch with Ron. v(t)·i(t)
integrated across an ideal switch's transition is ~0 (Vds jumps to 0 with no
v-i overlap), so a waveform integral would report ~0 switching loss for a
circuit that physically has real crossover loss. The waveform is used only
to locate switching events and measure the through-current at them; the loss
per event comes from the plan's first-order crossover model (TI Lakkas):

  E_sw ≈ ½ * Vds * I * (t_r + t_f)     per switch, per cycle

with crossover times from the Miller plateau: the driver delivers Qgd at
I_gate = (V_driver - V_plateau) / R_gate_drive, so t_cross ≈ Qgd / I_gate.
Qgd and V_plateau are exactly why the Phase 1 library carries them. Gate
energy is Qg * V_driver per switch per cycle. Vds across each switch in a
synchronous buck is Vin (HS blocks Vin while OFF; LS sees Vin while HS is
ON). I is the through-current at the event -- the inductor current, recovered
from the waveforms by integrating the inductor voltage:

  L * di_L/dt = v_sw - v_out - i_L * DCR

which is exact from the available node vectors (v_sw, v_out) -- recovering
i_L as v_out/R_load alone would miss the ripple current that flows into
Cout and understate conduction losses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from pyspice_openfoam_agent.library.schema import MOSFET
from pyspice_openfoam_agent.spice.runner import TransientResult

GATE_DRIVE_V = 5.0  # matches Phase 3 netlists' PULSE high level
GATE_DRIVE_R = 2.0  # ohm, typical controller gate-drive resistance

# numpy compat: np.trapz was renamed np.trapezoid in numpy 2.0. The project
# pins numpy<2.0 (trapz), but keep a shim so a future pin relax doesn't break.
_trapz = getattr(np, "trapezoid", np.trapz)


class LossExtractionError(ValueError):
    """Waveform data insufficient for loss extraction."""


@dataclass
class DeviceLosses:
    """Per-device average losses (W) at the simulated operating point."""

    hs_conduction: float
    ls_conduction: float
    inductor_dcr: float
    hs_switching: float
    ls_switching: float
    gate_drive: float  # both switches' gate drivers, total
    total: float = field(init=False)

    def __post_init__(self) -> None:
        self.total = (
            self.hs_conduction
            + self.ls_conduction
            + self.inductor_dcr
            + self.hs_switching
            + self.ls_switching
            + self.gate_drive
        )


@dataclass
class LossBreakdown:
    """Phase 5's output: localized heat sources for the thermal stage."""

    losses: DeviceLosses
    per_device_watts: dict[str, float]  # device tag -> W (thermal stage input)
    i_l_avg: float  # A, mean inductor current over the steady-state window
    i_l_peak: float  # A, peak |i_L| over the window (for Isat checks)
    notes: list[str] = field(default_factory=list)


def _crossover_time(mosfet: MOSFET) -> float:
    """First-order crossover time (t_r or t_f) from the Miller plateau:
    the driver delivers Qgd at I_gate = (V_driver - V_plateau) / R_gate.
    Clamped to a physically plausible 1..200 ns band for the library's
    silicon parts.
    """
    i_gate = (GATE_DRIVE_V - mosfet.V_plateau) / GATE_DRIVE_R
    t_cross = mosfet.Qgd / i_gate
    return float(np.clip(t_cross, 1e-9, 200e-9))


def _recover_inductor_current(
    t: np.ndarray, v_a: np.ndarray, v_b: np.ndarray, L: float, dcr: float
) -> np.ndarray:
    """Recover i_L(t) by integrating the inductor terminal voltage ODE:

        L * di_L/dt = v_a(t) - v_b(t) - i_L * DCR

    The inductor spans nodes (v_a, v_b), which DIFFER by topology:
      - buck:       across sw -> out         (v_sw - v_out)
      - boost:      across in -> sw          (v_in - v_sw)
      - buck_boost: across sw_a -> sw_b      (v_sw_a - v_sw_b), with the Rdcr
                                              element carrying sw_b -> out
    Passing the two terminal vectors directly makes this topology-agnostic.

    Exact discrete update per sample interval (semi-implicit in the DCR term,
    unconditionally stable):  i[k+1] = (i[k] + (v_a-v_b)[k]*dt/L)/(1 + DCR*dt/L)

    Initial condition i[0] = 0 matches Phase 4's runs (every transient starts
    from the zero state). The DCR term is a decaying low-pass on integration
    drift, so long steady-state windows do not accumulate unbounded error.
    """
    if len(t) < 2 or L <= 0:
        raise LossExtractionError("need >= 2 samples and positive L")
    dt = np.diff(t)
    if np.any(dt <= 0):
        raise LossExtractionError("non-monotonic time vector")
    v_l = v_a[:-1] - v_b[:-1]  # applied over [k, k+1]
    i = np.zeros_like(t, dtype=float)
    alpha = dcr / L
    for k in range(len(t) - 1):
        i[k + 1] = (i[k] + v_l[k] * dt[k] / L) / (1.0 + alpha * dt[k])
    return i


def _masked_mean_power(t: np.ndarray, p_instant: np.ndarray, mask: np.ndarray) -> float:
    """Average power over the window, integrating p only where mask is on
    (zero where off), normalized by the TOTAL window span. Correct for a
    device that dissipates only while ON: the ON-duty fraction is embedded.
    """
    if mask.sum() < 2:
        return 0.0
    p_eff = np.where(mask, p_instant, 0.0)
    span = float(t[-1] - t[0])
    if span <= 0:
        raise LossExtractionError("degenerate time span")
    return float(_trapz(p_eff, t) / span)


def _switch_masks(
    v_sw: np.ndarray, level_hi: float, steady_mask: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """HS-ON / LS-ON masks from the switch-node waveform. The switch node
    swings between two levels: the 'on' (charging) switch clamps it near the
    low/input rail, the 'off' (sync) switch clamps it near the high/output
    rail. Which voltage each corresponds to is topology-dependent, so the
    caller passes the HIGH rail level:

      - buck:       sw in {0, Vin}   -> HS on = sw~Vin,  LS on = sw~0
      - boost:      sw in {0, Vout}  -> ctrl on = sw~0,   sync on = sw~Vout
      - buck_boost: sw_a in {0, Vin} -> HS on = sw_a~Vin, LS_A on = sw_a~0
                    (the sw_b rail is the output side; only sw_a is thresholded
                     here for the two left switches — see extract_losses)
    Mid-level (transition) samples count as neither — ideal switches have
    zero-duration transitions, so no Ron conduction is charged during crossover.
    """
    hs_on = steady_mask & (v_sw > 0.7 * level_hi)
    ls_on = steady_mask & (v_sw < 0.3 * level_hi)
    return hs_on, ls_on


def extract_losses(
    result: TransientResult,
    mosfet: MOSFET,
    inductor_L: float,
    inductor_dcr: float,
    vin: float,
    vout: float,
    fsw: float,
    topology: str,
    settle_time: float,
) -> LossBreakdown:
    """Compute per-device losses from one steady-state transient result.

    `settle_time` comes from Phase 4's steady-state detector (cycle_time);
    everything before it is startup transient and must not pollute averages.
    Topology-aware since Phase 3's builders emit different node wiring:

      - buck:       L spans sw->out; HS/LS blocked cap = Vin; tags hs/ls
      - boost:      L spans in->sw;  ctrl/sync blocked cap = Vout; tags
                    ctrl->hs/sync->ls
      - buck_boost: 4 switches; the two left (HS/LS_A) carve sw_a in {0,Vin},
                    blocked cap = Vin+Vout; the two right (SYNC/LS_B) sit on
                    the sw_b/output rail. Reported as hs/ls for the left pair
                    (the dominant loss contributors) with the right pair
                    lumped into the same tags.
    """
    _topologies = {"buck", "boost", "buck_boost"}
    if topology not in _topologies:
        raise LossExtractionError(f"loss extraction supports {_topologies}, got {topology!r}")
    try:
        t = result.time
    except KeyError as e:
        raise LossExtractionError(f"missing waveform vector: {e}") from e

    steady_mask = t >= settle_time
    if steady_mask.sum() < 10:
        raise LossExtractionError(
            f"steady-state window too short ({steady_mask.sum()} samples after {settle_time:.3e} s)"
        )

    # --- topology-dependent node mapping + blocking voltage ---
    if topology == "buck":
        v_sw, v_lb = result["sw"], result["out"]   # L spans sw -> out
        level_hi = vin
        v_block = vin
    elif topology == "boost":
        v_sw, v_lb = result["in0"], result["sw"]  # L spans in0 -> sw
        level_hi = vout                           # sw swings {0, Vout}
        v_block = vout
    else:  # buck_boost
        v_sw, v_lb = result["sw_a"], result["sw_b"]  # L spans sw_a -> sw_b
        level_hi = vin                            # sw_a swings {0, Vin}
        v_block = vin + vout

    # Recover i_L over the FULL run first: the ODE initial condition i(0)=0
    # is only valid at the true simulation start (Phase 4 always starts from
    # the zero state). Slicing first would restart the integration mid-run
    # at a wrong initial current, and with the drift time constant
    # L/DCR (~340 us for these parts) far longer than the steady window, the
    # recovered current would be far from the real one (audit finding).
    i_l_full = _recover_inductor_current(t, v_sw, v_lb, L=inductor_L, dcr=inductor_dcr)

    # Now slice everything to the steady-state window: all averages below
    # normalize by the window span, so a long startup transient must not
    # dilute them (audit finding: full-span normalization made conduction
    # losses ~8x too small).
    i_l = i_l_full[steady_mask]
    t = t[steady_mask]
    v_sw = v_sw[steady_mask]

    hs_on, ls_on = _switch_masks(v_sw, level_hi, np.ones_like(t, dtype=bool))
    ron = mosfet.Rds_on

    p_hs_cond = _masked_mean_power(t, i_l**2 * ron, hs_on)
    p_ls_cond = _masked_mean_power(t, i_l**2 * ron, ls_on)
    p_dcr = _masked_mean_power(t, i_l**2 * inductor_dcr, np.ones_like(t, dtype=bool))

    # --- switching loss (crossover model) ---
    i_window = np.abs(i_l)
    i_event = float(np.mean(i_window))
    t_r = _crossover_time(mosfet)
    t_f = t_r  # symmetric first-order model; t_r+t_f = 2*t_cross
    e_sw_per_switch = 0.5 * v_block * i_event * (t_r + t_f)
    p_sw = e_sw_per_switch * fsw  # one turn-on + one turn-off pair per cycle

    p_gate = 2.0 * mosfet.Qg * GATE_DRIVE_V * fsw  # both switches' drivers

    dl = DeviceLosses(
        hs_conduction=p_hs_cond,
        ls_conduction=p_ls_cond,
        inductor_dcr=p_dcr,
        hs_switching=p_sw,
        ls_switching=p_sw,
        gate_drive=p_gate,
    )

    notes = [
        f"crossover t_r = t_f = {t_r * 1e9:.1f} ns (from Qgd={mosfet.Qgd * 1e9:.1f} nC, "
        f"V_plateau={mosfet.V_plateau} V, R_gate={GATE_DRIVE_R} ohm)",
        f"event current = mean |i_L| = {i_event:.2f} A over the steady-state window",
        "synchronous-only accounting (no body-diode term) per project decision",
        f"blocking voltage {v_block:.1f} V ({topology}); switch node swings to "
        f"{'Vin' if topology=='buck' else vout if topology=='boost' else 'Vin (left pair)'}",
    ]

    per_device = {
        "hs_mosfet": dl.hs_conduction + dl.hs_switching + dl.gate_drive / 2,
        "ls_mosfet": dl.ls_conduction + dl.ls_switching + dl.gate_drive / 2,
        "inductor": dl.inductor_dcr,
    }
    return LossBreakdown(
        losses=dl,
        per_device_watts=per_device,
        i_l_avg=float(np.mean(i_l)),
        i_l_peak=float(np.max(np.abs(i_l))),
        notes=notes,
    )