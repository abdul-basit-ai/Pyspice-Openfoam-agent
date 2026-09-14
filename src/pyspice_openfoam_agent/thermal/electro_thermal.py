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


MAX_ITER = 5
TOL_DEGC = 2.0


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
) -> float:
    """Total dissipation (W) at junction temperature tj_c.

    Rds_on scales with the database tempco, folded into conduction loss; a
    small hard-switching term and gate term keep the model well-posed. This
    mirrors Phase 5's separation of conduction vs switching but at reduced
    order (no per-cycle waveform integration).
    """
    rds = rds_on_25 * (1 + tempco_ppm * 1e-6 * (tj_c - 25.0))
    rds = max(rds, 1e-6)
    p_cond = iout ** 2 * rds * 1.5  # HS+LS conduction factor (duty + ripple)
    p_sw = 0.5 * 12.0 * iout * 2 * 38e-9 * fsw_hz  # crossover model, 12 V ref
    p_gate = 2 * 5.0 * 2.0 * 1e-9 * fsw_hz  # small gate-drive term
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
    ambient_c: float = 27.0,
    tol: float = TOL_DEGC,
    max_iter: int = MAX_ITER,
) -> ConvergenceResult:
    """Fixed-point electro-thermal convergence to the self-consistent Tj.

    Start at ambient, iterate losses(Tj_k) -> Tj_{k+1}. Converged when
    |Tj_{k+1} - Tj_k| < tol; else validation failure at max_iter.
    """
    tj = ambient_c
    history: list[str] = [f"iter  0: Tj = {tj:.2f} degC (seed at ambient)"]
    feedback: float = 0.0
    for it in range(1, max_iter + 1):
        p_loss = _loss_vs_tj(mosfet_pn, iout, fsw_hz, tj,
                             rds_on_25, tempco_ppm, id_max)
        tj_new = reduced_order_tj(p_loss, r_theta_ja, v_in_m_s, ambient_c)
        dtj = tj_new - tj
        history.append(
            f"iter {it:2d}: P = {p_loss:.3f} W, Tj = {tj_new:6.2f} degC, "
            f"dTj = {dtj:+6.2f} degC")
        tj = tj_new
        feedback = dtj
        if abs(dtj) < tol:
            return ConvergenceResult(
                converged=True, final_tj_c=round(tj, 2), iterations=it,
                tj_history=history,
                reason=f"converged: |dTj|={abs(dtj):.2f} degC < {tol} degC "
                       f"at iteration {it}")
    return ConvergenceResult(
        converged=False, final_tj_c=round(tj, 2), iterations=max_iter,
        tj_history=history,
        reason=f"FAILED to converge in {max_iter} iterations "
               f"(last |dTj|={abs(feedback):.2f} degC >= {tol} degC) — "
               "validation failure; escalate to investigation")


# ---- convenience wrapper from a Requirements + library part ----

def converge_for_design(
    req: Requirements,
    mosfet,
    v_in_m_s: float = 1.0,
) -> ConvergenceResult:
    """Drive convergence from a Phase 1 library MOSFET's fields directly."""
    return converge(
        mosfet_pn=mosfet.part_number,
        iout=req.Iout,
        fsw_hz=req.fsw_khz * 1e3,
        rds_on_25=mosfet.Rds_on,
        tempco_ppm=mosfet.Rds_on_tempco_ppm,
        id_max=mosfet.Id_max,
        r_theta_ja=mosfet.R_theta_ja,
        v_in_m_s=v_in_m_s,
    )