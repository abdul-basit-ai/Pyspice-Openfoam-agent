"""Phase 13 (rework, gap #2): explicit electro-thermal convergence loop.

The Phase 12/9 loop-back edge requires: recompute Rds_on/Vf and core losses
at the newly found Tj, rerun losses -> thermal, and iterate until the
junction temperature stops moving. This is a fixed-point iteration:

    Tj_{k+1} = f( Tj_k )   where  f transforms
                  losses(Tj_k)  ->  thermal(losses(Tj_k))

Convergence criteria (goals gap #2): capped at MAX_ITER iterations (5) and
converged when |dTj| between consecutive iterations < TOL (2 degC). If it
fails to converge, treat as a validation failure per the "large discrepancies
must trigger investigation" policy — the caller (orchestrator/HITL gate) must
not silently accept the last iterate.

The physics is deterministic and reduced-order (suitable for the search tier
of the two-tier thermal policy): conduction loss scales with Rds_on(Tj) using
the temperature coefficient from the Phase 1 database; the junction
temperature follows the airflow-scaled R_theta_ja spreading-resistance model.
Full CHT verifies only the converged finalists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyspice_openfoam_agent.design.object import Requirements
from pyspice_openfoam_agent.optimization.pareto import reduced_order_tj


class ElectroThermalError(RuntimeError):
    """Convergence failed or the model is degenerate."""


MAX_ITER = 8
TOL_DEGC = 2.0

# Topology-aware conduction factor for the reduced-order model. The blanket
# 1.5 ("duty + ripple") was wrong for every topology (audit fix): the
# duty-weighted HS+LS conduction of a synchronous buck is exactly Iout^2*Rds
# (D + (1-D) = 1); boost/buck-boost switches carry the full inductor current
# Iout/(1-D), so their factor is 1/(1-D)^2 (x2 for the 4-switch
# buck-boost, whose two series switches conduct in each phase).
def conduction_factor(topology: str, D: float) -> float:
    D = min(max(D, 0.05), 0.95)
    if topology == "boost":
        return 1.0 / (1.0 - D) ** 2
    if topology == "buck_boost":
        return 2.0 / (1.0 - D) ** 2
    return 1.0  # buck


@dataclass
class ConvergenceResult:
    converged: bool
    final_tj_c: float
    iterations: int
    tj_history: list[str] = field(default_factory=list)  # human-readable trace
    reason: str = ""


def _loss_vs_tj(
    mosfet_pn: str, iout: float, fsw_hz: float, tj_c: float,
    rds_on_25: float, tempco_ppm: float, id_max: float,
    v_block: float = 12.0, t_cross_s: float = 38e-9, qg_c: float = 2e-9,
    n_switches: float = 2.0, conduction_factor: float = 1.0,
) -> float:
    """Total dissipation (W) at junction temperature tj_c.

    Rds_on scales with the database tempco, folded into conduction loss with
    the topology-derived `conduction_factor` (see conduction_factor()); the
    hard-switching term uses the part's ACTUAL blocking voltage and crossover
    time, and the gate term its actual Qg (audit fix: v_block was pinned at
    12 V, t_cross at 38 ns and Qg at 2 nC for every design — a 48 V converter
    under-predicted switching loss ~4x). `mosfet_pn`/`id_max` are carried for
    provenance/logging by direct callers and play no role in the math.
    """
    rds = rds_on_25 * (1 + tempco_ppm * 1e-6 * (tj_c - 25.0))
    rds = max(rds, 1e-6)
    p_cond = iout ** 2 * rds * conduction_factor
    p_sw = 0.5 * v_block * iout * 2 * t_cross_s * fsw_hz  # crossover model
    p_gate = n_switches * 5.0 * qg_c * fsw_hz  # gate-drive term
    return p_cond + p_sw + p_gate


def converge(
    mosfet_pn: str,
    iout: float,
    fsw_hz: float,
    rds_on_25: float,
    tempco_ppm: float,
    id_max: float,
    r_theta_ja: float,
    v_in_m_s: float = 1.0,
    ambient_c: float = 25.0,  # matches the CHT tier (board.AMBIENT_TEMP_C)
    tol: float = TOL_DEGC,
    max_iter: int = MAX_ITER,
    v_block: float = 12.0,
    t_cross_s: float = 38e-9,
    qg_c: float = 2e-9,
    n_switches: float = 2.0,
    conduction_factor: float = 1.0,
) -> ConvergenceResult:
    """Fixed-point electro-thermal convergence to the self-consistent Tj.

    Start at ambient, iterate losses(Tj_k) -> Tj_{k+1}. Converged when
    |Tj_{k+1} - Tj_k| < tol; else validation failure at max_iter.

    The iteration is ADAPTIVELY DAMPED (audit fix: the undamped fixed point
    diverges whenever the loop gain R_theta * dP/dTj > 1 — a 10 A / 50 mOhm /
    8000 ppm design has gain ~3.6 and oscillated forever, always reported as
    a validation failure). The relaxation starts at 1.0 and halves whenever
    the residual grows, so high-gain designs converge in the same bounded
    budget while low-gain ones keep the original single-step behavior.
    MAX_ITER was raised 5 -> 8 to leave room for the damping discovery steps.
    """
    tj = ambient_c
    history: list[str] = [f"iter  0: Tj = {tj:.2f} degC (seed at ambient)"]
    relax = 1.0
    prev_residual: float | None = None
    feedback: float = 0.0
    for it in range(1, max_iter + 1):
        p_loss = _loss_vs_tj(mosfet_pn, iout, fsw_hz, tj,
                             rds_on_25, tempco_ppm, id_max,
                             v_block=v_block, t_cross_s=t_cross_s,
                             qg_c=qg_c, n_switches=n_switches,
                             conduction_factor=conduction_factor)
        tj_raw = reduced_order_tj(p_loss, r_theta_ja, v_in_m_s, ambient_c)
        residual = tj_raw - tj          # undamped map residual
        tj_new = tj + relax * residual  # damped step
        dtj = tj_new - tj
        history.append(
            f"iter {it:2d}: P = {p_loss:.3f} W, Tj = {tj_new:6.2f} degC, "
            f"residual = {residual:+6.2f} degC, relax = {relax:.2f}")
        tj = tj_new
        feedback = residual
        if abs(residual) < tol:
            return ConvergenceResult(
                converged=True, final_tj_c=round(tj, 2), iterations=it,
                tj_history=history,
                reason=f"converged: |residual|={abs(residual):.2f} degC < {tol} degC "
                       f"at iteration {it}")
        # adaptive damping: back off when the iteration is being amplified
        if prev_residual is not None and abs(residual) > abs(prev_residual):
            relax = max(relax * 0.5, 0.1)
        prev_residual = residual
    return ConvergenceResult(
        converged=False, final_tj_c=round(tj, 2), iterations=max_iter,
        tj_history=history,
        reason=f"FAILED to converge in {max_iter} iterations "
               f"(last |residual|={abs(feedback):.2f} degC >= {tol} degC) — "
               "validation failure; escalate to investigation")


# ---- convenience wrapper from a Requirements + library part ----

def converge_for_design(
    req: Requirements,
    mosfet,
    v_in_m_s: float = 1.0,
    topology: str | None = None,
) -> ConvergenceResult:
    """Drive convergence from a Phase 1 library MOSFET's fields directly.

    The switching-loss constants come from the ACTUAL design (audit fix: the
    old path pinned v_block=12 V / t_cross=38 ns / Qg=2 nC for everything):
    blocking voltage is topology-aware (buck: Vin; boost: Vout; buck_boost:
    max), crossover time from the part's Qgd/V_plateau (same formula as
    spice/losses.py — the shared public crossover_time), Qg from the part.
    """
    from pyspice_openfoam_agent.spice.losses import crossover_time

    if topology is None:
        ratio = req.Vout / req.Vin if req.Vin else 1.0
        topology = "buck" if ratio < 0.95 else "boost" if ratio > 1.05 else "buck_boost"
    v_block = {
        "buck": req.Vin, "boost": req.Vout,
        "buck_boost": max(req.Vin, req.Vout),
    }[topology]
    n_switches = 4.0 if topology == "buck_boost" else 2.0
    D = {
        "buck": req.Vout / max(req.Vin, 1e-12),
        "boost": 1.0 - min(req.Vin / max(req.Vout, 1e-12), 0.999),
        "buck_boost": req.Vout / (req.Vin + req.Vout),
    }[topology]
    return converge(
        mosfet_pn=mosfet.part_number,
        iout=req.Iout,
        fsw_hz=req.fsw_khz * 1e3,
        rds_on_25=mosfet.Rds_on,
        tempco_ppm=mosfet.Rds_on_tempco_ppm,
        id_max=mosfet.Id_max,
        r_theta_ja=mosfet.R_theta_ja,
        v_in_m_s=v_in_m_s,
        v_block=v_block,
        t_cross_s=crossover_time(mosfet),
        qg_c=mosfet.Qg,
        n_switches=n_switches,
        conduction_factor=conduction_factor(topology, D),
    )