"""Phase 5: per-device loss extraction for the thermal stage.

Loss accounting for the synchronous converter topologies Phase 3 emits
(HS + LS MOSFET pair modeled as ideal switches with the real part's Ron,
real DCR/ESR parasitics). Per-device, per steady-state period:

  P_hs_cond  = <i_L² * Ron> while the HS switch conducts
  P_ls_cond  = <i_L² * Ron> while the LS switch is ON
  P_l_dcr    = <i_L² * DCR> (continuous)
  P_sw_hs    = ½*Vblock*I*(t_r+t_f)*fsw  +  ½*Coss*Vblock²*fsw
               (hard-switch crossover + output-capacitance, HS only)
  P_sw_ls    = V_F*i_L*3*t_dead*fsw  +  Qrr*Vblock*fsw
               (body-diode conduction over the 3 dead-time windows/period the
               gate phasing opens + reverse recovery, LS only)
  P_gate     = Qg * V_driver * fsw per driven switch

HS and LS switching are DECOUPLED (correct physics): the HS switch is the
hard-switcher (crossover + Coss loss); the synchronous LS switch turns on with
~0 V across it after dead time, so its hard-switch crossover is negligible and
its loss is body-diode dead-time conduction + reverse recovery instead.

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
from pyspice_openfoam_agent.netlist.builder import GATE_DRIVE_R, GATE_DRIVE_V, dead_time_for
from pyspice_openfoam_agent.spice.runner import TransientResult

__all__ = [
    "GATE_DRIVE_V",
    "GATE_DRIVE_R",
    "DeviceLosses",
    "LossBreakdown",
    "LossExtractionError",
    "crossover_time",
    "efficiency_from_losses",
    "extract_losses",
]

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
    capacitor_esr: float = 0.0  # output-cap bank RMS ripple current x ESR
    total: float = field(init=False)

    def __post_init__(self) -> None:
        self.total = (
            self.hs_conduction
            + self.ls_conduction
            + self.inductor_dcr
            + self.hs_switching
            + self.ls_switching
            + self.gate_drive
            + self.capacitor_esr
        )


@dataclass
class LossBreakdown:
    """Phase 5's output: localized heat sources for the thermal stage."""

    losses: DeviceLosses
    per_device_watts: dict[str, float]  # device tag -> W (thermal stage input)
    i_l_avg: float  # A, mean inductor current over the steady-state window
    i_l_peak: float  # A, peak |i_L| over the window (for Isat checks)
    notes: list[str] = field(default_factory=list)


def efficiency_from_losses(
    vout: float, iout: float, losses_w: float,
) -> float:
    """Conversion efficiency from output power and total loss.

        eta = P_out / (P_out + P_loss),   P_out = |Vout| * Iout

    A single normative formula (no hardcoded scaling / division). Guarantees
    the physically-required 0 < eta <= 1, raising rather than silently
    returning an impossible number (task assertion).
    """
    p_out = abs(vout) * max(iout, 0.0)
    if p_out <= 0 or losses_w < 0:
        raise LossExtractionError(
            f"cannot compute efficiency: P_out={p_out:.3g} W, P_loss={losses_w:.3g} W "
            "(need P_out>0 and P_loss>=0)"
        )
    eta = p_out / (p_out + losses_w)
    if not (0.0 < eta <= 1.0 + 1e-12):
        raise LossExtractionError(
            f"efficiency {eta:.4f} outside (0, 1] — loss physics corrupted "
            f"(P_out={p_out:.3g}, P_loss={losses_w:.3g})"
        )
    return float(eta)


def crossover_time(mosfet: MOSFET) -> float:
    """First-order crossover time (t_r or t_f) from the Miller plateau:
    the driver delivers Qgd at I_gate = (V_driver - V_plateau) / R_gate.
    Clamped to a physically plausible 1..200 ns band for the library's
    silicon parts. (Public: shared with the reduced-order electro-thermal
    tier so the two loss models cannot silently diverge.)
    """
    i_gate = (GATE_DRIVE_V - mosfet.V_plateau) / GATE_DRIVE_R
    # guard: a V_plateau at/above the drive rail (V_plateau=5.0 parts on a
    # 5 V drive) gives ZERO gate current -> unclippable division by zero
    t_cross = mosfet.Qgd / max(i_gate, 1e-9)
    return float(np.clip(t_cross, 1e-9, 200e-9))


_crossover_time = crossover_time  # internal historical name


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
    v_sw: np.ndarray, level_hi: float, steady_mask: np.ndarray, topology: str
) -> tuple[np.ndarray, np.ndarray]:
    """(hard_on, sync_on) masks from the switch-node waveform, where 'hard' is
    the HARD-SWITCHING control device and 'sync' the synchronous rectifier.
    Which rail each corresponds to is topology-dependent, so the caller passes
    the HIGH rail level:

      - buck:       sw in {0, Vin}    -> hard(HS) on = sw~Vin,  sync(LS) on = sw~0
      - boost:      sw in {0, Vout}   -> hard(ctrl) on = sw~0,  sync on = sw~Vout
      - buck_boost: sw_a in {0, Vin}  -> charge phase (HS+LS_B) = sw_a~Vin,
                    discharge phase (LS_A+SYNC) = sw_a~0

    Dead-time intervals are EXCLUDED from both masks: the body diode clamps
    the node ~0.7 V BEYOND the rail (buck/buck_boost low gap: sw ~ -0.7 V;
    boost/buck_boost high gap: sw ~ rail + 0.7 V), and those samples belong
    to the explicit body-diode loss term, not to Ron conduction (audit fix:
    they were double-counted before). Mid-level (transition) samples count as
    neither — ideal switches have zero-duration transitions.
    """
    if topology == "buck":
        hard_on = steady_mask & (v_sw > 0.7 * level_hi) & (v_sw < level_hi + 0.35)
        sync_on = steady_mask & (v_sw > -0.35) & (v_sw < 0.3 * level_hi)
    elif topology == "boost":
        # ctrl (the hard switcher) conducts with sw clamped to ~0; the sync
        # switch conducts with sw at ~Vout. Masks were previously swapped
        # (audit fix): hs/l conduction went to the wrong physical die.
        hard_on = steady_mask & (v_sw < 0.3 * level_hi)
        sync_on = steady_mask & (v_sw > 0.7 * level_hi) & (v_sw < level_hi + 0.35)
    else:  # buck_boost (left switch node sw_a)
        hard_on = steady_mask & (v_sw > 0.7 * level_hi) & (v_sw < level_hi + 0.35)
        sync_on = steady_mask & (v_sw > -0.35) & (v_sw < 0.3 * level_hi)
    return hard_on, sync_on


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
    cap_esr: float = 0.0,
    iout: float | None = None,
    mosfet_ls: MOSFET | None = None,
) -> LossBreakdown:
    """Compute per-device losses from one steady-state transient result.

    `settle_time` comes from Phase 4's steady-state detector (cycle_time);
    everything before it is startup transient and must not pollute averages.
    Topology-aware since Phase 3's builders emit different node wiring:

      - buck:       L spans sw->out; HS/LS blocked cap = Vin; tags hs/ls
      - boost:      L spans in->sw;  ctrl/sync blocked cap = Vout; the ctrl
                    (hard-switching) device maps to the hs tag, sync -> ls
      - buck_boost: 4-switch non-inverting; L+DCR span sw_a->sw_b. The charge
                    phase (HS+LS_B, sw_a~Vin) and discharge phase (LS_A+SYNC,
                    sw_a~0) each carry i_L through TWO series switches, so
                    each phase's conduction integral is doubled. Blocked cap
                    = max(Vin, Vout) (left pair blocks Vin, right pair Vout).
    """
    _topologies = {"buck", "boost", "buck_boost"}
    if topology not in _topologies:
        raise LossExtractionError(f"loss extraction supports {_topologies}, got {topology!r}")
    if settle_time is None:
        raise LossExtractionError(
            "transient never reached steady state (detector returned no cycle "
            "time) — rerun with more cycles or investigate; losses over a "
            "non-settled window are meaningless"
        )
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
        v_sw, v_lb = result["sw_a"], result["sw_b"]  # L+DCR span sw_a -> sw_b
        level_hi = vin                            # sw_a swings {0, Vin}
        v_block = max(vin, vout)                  # left pair blocks Vin, right Vout

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

    hs_on, ls_on = _switch_masks(v_sw, level_hi, np.ones_like(t, dtype=bool), topology)

    # Per-switch support: mosfet_ls (when given) is the OTHER switch
    # position's part. The mask that carries the HARD device uses mosfet's
    # Ron; the mask that carries the SYNC device uses mosfet_ls's Ron.
    ls_part = mosfet_ls if mosfet_ls is not None else mosfet
    ron_hard = mosfet.Rds_on
    ron_sync = ls_part.Rds_on

    if topology == "buck_boost":
        # each phase carries i_L through TWO series switches of its OWN leg
        # (charge: HS+LS_B both leg-1 parts; discharge: LS_A+SYNC both leg-2)
        p_hs_cond = _masked_mean_power(
            t, i_l**2 * (ron_hard + ron_hard), hs_on)
        p_ls_cond = _masked_mean_power(
            t, i_l**2 * (ron_sync + ron_sync), ls_on)
    else:
        p_hs_cond = _masked_mean_power(t, i_l**2 * ron_hard, hs_on)
        p_ls_cond = _masked_mean_power(t, i_l**2 * ron_sync, ls_on)
    p_dcr = _masked_mean_power(t, i_l**2 * inductor_dcr, np.ones_like(t, dtype=bool))

    # --- capacitor ESR loss (Phase 9: losses localized by component) ---
    # i_cap = feed current into the output node minus the load current.
    # buck: the inductor feeds the cap/load node continuously (i_feed = i_L).
    # boost/buck_boost: the output node is fed only while the synchronous
    # rectifier conducts (the ls_on mask) — that pulsed diode-side current is
    # what actually stresses the cap (its RMS is far above Iout).
    p_cap_esr = 0.0
    i_cap_rms = 0.0
    if cap_esr > 0 and iout is not None and iout > 0 and result.has("out"):
        v_out_w = np.asarray(result["out"])[steady_mask]
        i_out_w = v_out_w * (iout / vout)  # load current ≈ v_out / R_load
        if topology == "buck":
            i_feed = i_l
        else:
            i_feed = np.where(ls_on, i_l, 0.0)
        i_cap = i_feed - i_out_w
        i_cap_rms = float(np.sqrt(np.mean(i_cap**2)))
        p_cap_esr = i_cap_rms**2 * cap_esr

    # --- switching loss: DECOUPLED HS vs LS mechanisms (task C) ---
    # HS: hard-switching crossover + output-capacitance (Coss) charge/discharge.
    # The through-current at each event is the inductor RIPPLE ENDPOINT, not
    # the window mean: the HS turns ON at the valley (i_L minimum) and OFF at
    # the peak (i_L maximum) in CCM for every supported topology (audit fix:
    # mean |i_L| biased crossover loss toward the average instead of the
    # actual commutation currents).
    i_on = float(np.min(i_l))   # valley at HS turn-on
    i_off = float(np.max(i_l))  # peak at HS turn-off
    t_r = _crossover_time(mosfet)
    t_f = t_r  # symmetric first-order model; t_r+t_f = 2*t_cross
    p_sw_hs = 0.5 * v_block * (abs(i_on) + abs(i_off)) * t_r * fsw
    # Coss energy: E_oss ~ 0.5*Coss*V_block^2, paid once per hard turn-on cycle.
    # Only counted when the part's Coss is actually in the library — a
    # fabricated generic value would silently invent switching loss (audit
    # fix: the old 500 pF default fabricated a number no datasheet gave).
    coss_notes: list[str] = []
    if mosfet.Coss and mosfet.Coss > 0:
        p_oss = 0.5 * mosfet.Coss * v_block**2 * fsw
    else:
        p_oss = 0.0
        coss_notes.append("Coss not in library — output-capacitance loss term omitted")
    p_hs_switching = p_sw_hs + p_oss  # hard-switch + output-capacitance loss

    # LS (synchronous rectifier): near-ZERO hard-switching crossover (it turns
    # on with ~0 V across it after dead time). Its losses are body-diode
    # conduction during dead time + reverse recovery (Qrr):
    # Every builder's gate phasing opens THREE dead-time windows per period
    # (2*td after the hard switch turns off + td before the period wraps), and
    # the body diode freewheels the inductor current through all of them. The
    # dead time comes from the SAME builder function the netlist was generated
    # with, so the loss model can never drift from the simulated gate timing
    # (audit fix: stale getattr default).
    t_dead = max(dead_time_for(mosfet), dead_time_for(ls_part))
    p_ls_diode = ls_part.V_F * abs(i_on + i_off) / 2.0 * 3.0 * t_dead * fsw
    qrr = ls_part.Qrr if ls_part.Qrr else 0.0
    p_ls_rr = qrr * v_block * fsw
    p_ls_switching = p_ls_diode + p_ls_rr  # body-diode + reverse-recovery

    # Gate-drive loss: one driver per driven switch — 2 for buck/boost,
    # 4 for the 4-switch buck_boost (audit fix) — each at its own part's Qg.
    n_drivers = 4.0 if topology == "buck_boost" else 2.0
    p_gate = ((n_drivers / 2.0) * mosfet.Qg
              + (n_drivers / 2.0) * ls_part.Qg) * GATE_DRIVE_V * fsw

    dl = DeviceLosses(
        hs_conduction=p_hs_cond,
        ls_conduction=p_ls_cond,
        inductor_dcr=p_dcr,
        hs_switching=p_hs_switching,
        ls_switching=p_ls_switching,
        gate_drive=p_gate,
        capacitor_esr=p_cap_esr,
    )

    notes = [
        f"crossover t_r = t_f = {t_r * 1e9:.1f} ns (from Qgd={mosfet.Qgd * 1e9:.1f} nC, "
        f"V_plateau={mosfet.V_plateau} V, R_gate={GATE_DRIVE_R} ohm)",
        f"event currents: valley {i_on:.2f} A (turn-on), peak {i_off:.2f} A (turn-off) "
        f"over the steady-state window",
        f"dead time {t_dead * 1e9:.0f} ns x 3 windows/period (netlist gate phasing)",
        f"blocking voltage {v_block:.1f} V ({topology}); switch node swings to "
        f"{'Vin' if topology=='buck' else vout if topology=='boost' else 'Vin (left pair)'}",
    ]
    notes.extend(coss_notes)
    if p_cap_esr > 0:
        notes.append(
            f"output capacitor ESR loss {p_cap_esr * 1e3:.1f} mW "
            f"(I_cap,rms {i_cap_rms:.2f} A x ESR {cap_esr * 1e3:.2f} mOhm) — "
            f"dissipated in the cap/bank, board-level heat (no die zone)"
        )
    if topology == "buck_boost":
        notes.append(
            "4-switch topology: each phase's conduction counted for its two series "
            "switches; thermal zones split total MOSFET heat evenly (2 zones, 4 dies)"
        )

    # Thermal heat zones. buck/buck_boost: the two zones represent the switch
    # pair; for buck_boost the 4 dies are split across the 2 available zones
    # with the TOTAL preserved (conduction split evenly — see note above).
    if topology == "buck_boost":
        half_cond = (dl.hs_conduction + dl.ls_conduction) / 2.0
        per_device = {
            "hs_mosfet": half_cond + dl.hs_switching + dl.gate_drive / 2,
            "ls_mosfet": half_cond + dl.ls_switching + dl.gate_drive / 2,
            "inductor": dl.inductor_dcr,
        }
    else:
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