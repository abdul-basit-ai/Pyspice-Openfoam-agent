"""Phase 6: fast electrical screening — reject doomed designs before SPICE.

Sits between component selection (Phase 5) and the SPICE transient (Phase
7). Uses closed-form stress/loss estimates on the SELECTED real parts. Per
the goals' policy: clearly-violating designs are rejected here; borderline
designs continue to SPICE rather than being rejected on a rough model.

Rejected reasons are returned, not raised — the caller (orchestrator/UI)
decides whether to show them, auto-fix, or ask the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyspice_openfoam_agent.sizing.engine import SizingResult
from pyspice_openfoam_agent.library.schema import MOSFET, Capacitor, Inductor


@dataclass
class ScreeningVerdict:
    passed: bool
    rejected: bool  # clearly violates a hard constraint
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # borderline: continue to SPICE


def screen(
    spec_vin: float,
    spec_vout: float,
    spec_iout: float,
    fsw: float,
    sizing: SizingResult,
    mosfet: MOSFET,
    inductor: Inductor,
    capacitor: Capacitor,
    tj_limit_c: float = 150.0,
    ambient_c: float = 27.0,
    vripple_budget: float | None = None,
) -> ScreeningVerdict:
    """Fast screening on the selected real parts. Pure math, no simulation.

    `vripple_budget`: the spec's output ripple budget (V). When given, the
    predicted peak-to-peak output ripple (capacitive + ESR terms) must meet
    it — the audit-run boost shipped a 3x ripple-spec violation because
    C_min sizing is capacitive-only and nothing compared ESR ripple against
    the budget. Pass None only in legacy direct callers without a budget.
    """
    reasons: list[str] = []
    warnings: list[str] = []

    # --- voltage stress on the MOSFET (buck: Vin + ringing headroom) ---
    v_stress = spec_vin * 1.2  # 20% ringing headroom
    if mosfet.Vds_max < v_stress:
        reasons.append(
            f"MOSFET Vds_max {mosfet.Vds_max:.0f}V < switch stress {v_stress:.0f}V "
            f"(Vin {spec_vin:.0f}V + 20% headroom)"
        )

    # --- current stress ---
    if sizing.I_peak > mosfet.Id_max:
        reasons.append(
            f"MOSFET Id_max {mosfet.Id_max:.0f}A < I_peak {sizing.I_peak:.1f}A"
        )

    # --- inductor saturation ---
    if sizing.I_peak > inductor.Isat:
        reasons.append(
            f"Inductor Isat {inductor.Isat:.1f}A < I_peak {sizing.I_peak:.1f}A "
            "(inductor will saturate)"
        )
    elif sizing.I_peak > inductor.Isat * 0.9:
        warnings.append(
            f"Inductor Isat {inductor.Isat:.1f}A within 10% of I_peak "
            f"{sizing.I_peak:.1f}A — borderline, continuing to SPICE"
        )

    # --- capacitor voltage rating ---
    if capacitor.V_rated < spec_vout * 1.2:
        reasons.append(
            f"Capacitor V_rated {capacitor.V_rated:.0f}V < Vout {spec_vout:.0f}V + 20%"
        )

    # --- capacitor ripple current (fast estimate: ripple current ~ di_pp/sqrt(12)) ---
    i_ripple_cap = sizing.di_pp / (12 ** 0.5)
    if i_ripple_cap > capacitor.Irms_max:
        warnings.append(
            f"Capacitor Irms_max {capacitor.Irms_max:.1f}A < estimated ripple current "
            f"{i_ripple_cap:.2f}A — borderline, continuing to SPICE"
        )

    # --- output ripple vs the SPEC budget (audit fix) ---
    # C_min sizing is capacitive-only; for real parts the ESR term usually
    # dominates (e.g. a 10 mOhm POSCAP at a boost's ~I_L cap-current swing
    # costs ~90 mV on its own). Predict p-p ripple from the selected part:
    #   buck:            cap current swings by di_pp
    #   boost/buck_boost: cap current swings by ~I_L_avg = Iout/(1-D)
    if vripple_budget is not None and vripple_budget > 0:
        if sizing.topology == "buck":
            i_cap_pp = sizing.di_pp
            v_cap_pp = sizing.di_pp / (8.0 * fsw * capacitor.C)
        else:
            d = min(max(sizing.D, 0.05), 0.95)
            i_cap_pp = spec_iout / (1.0 - d)
            v_cap_pp = spec_iout * d / (fsw * capacitor.C)
        v_esr_pp = capacitor.ESR * i_cap_pp
        v_pp_pred = v_cap_pp + v_esr_pp
        if v_pp_pred > vripple_budget:
            reasons.append(
                f"predicted output ripple {v_pp_pred * 1e3:.1f} mV pp exceeds the "
                f"spec budget {vripple_budget * 1e3:.1f} mV "
                f"(ESR term {v_esr_pp * 1e3:.1f} mV from {capacitor.ESR * 1e3:.1f} mOhm "
                f"x {i_cap_pp:.2f} A cap-current swing + capacitive "
                f"{v_cap_pp * 1e3:.1f} mV) — select a lower-ESR / larger capacitor "
                f"or relax the budget")
        elif v_pp_pred > 0.8 * vripple_budget:
            warnings.append(
                f"predicted output ripple {v_pp_pred * 1e3:.1f} mV pp is within 20% "
                f"of the {vripple_budget * 1e3:.1f} mV budget — SPICE will verify")

    # --- conduction-loss efficiency floor (best-case efficiency estimate) ---
    r_total = mosfet.Rds_on + inductor.DCR
    i2 = spec_iout ** 2
    p_cond = i2 * r_total  # crude: assumes one FET conducting at a time, full duty
    p_out = spec_vout * spec_iout
    eff_best = p_out / (p_out + p_cond) if p_out > 0 else 0.0
    # best-case conduction-only efficiency: if even THIS is below a hard floor,
    # the design is clearly bad. Borderline low values continue to SPICE.
    if eff_best < 0.5:
        reasons.append(
            f"Best-case conduction-only efficiency {eff_best * 100:.0f}% below 50% floor "
            f"(Rds_on+DCR = {r_total * 1e3:.1f} mOhm at {spec_iout:.0f}A)"
        )
    elif eff_best < 0.75:
        warnings.append(
            f"Conduction-only efficiency {eff_best * 100:.0f}% is low — SPICE will refine"
        )

    # --- self-heating floor: Tj estimate from conduction loss alone ---
    # even perfect spreading cannot beat R_theta_ja with only conduction loss
    tj_floor = ambient_c + p_cond * mosfet.R_theta_ja * 0.5  # 0.5 = split over 2 FETs
    if tj_floor > tj_limit_c:
        reasons.append(
            f"Conduction-loss Tj floor {tj_floor:.0f}degC > {tj_limit_c:.0f}degC limit "
            f"(R_theta_ja {mosfet.R_theta_ja:.0f} degC/W is thermally infeasible)"
        )

    return ScreeningVerdict(
        passed=len(reasons) == 0,
        rejected=len(reasons) > 0,
        reasons=reasons,
        warnings=warnings,
    )