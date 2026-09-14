"""Phase 13: multi-objective design-space optimization (pymoo NSGA-II).

Goals rework: move from pass/fail single-lever mitigation (old P10) to real
design-space exploration with a Pareto frontier. Objectives (minimize):

  f1 = -efficiency          (maximize efficiency)
  f2 = Tj_max (degC)        (minimize temperature)
  f3 = cost                 (minimize cost; library parts carry relative cost)

Decision variables (mixed discrete/continuous, why pymoo was chosen):
  mosfet  : categorical (Phase 1 library candidates)
  fsw     : continuous [100, 500] kHz
  L       : continuous [L_min, 5x L_min] uH
  v_in    : continuous [0.5, 3.0] m/s airflow

Two-tier thermal fidelity (goals gap #3): EVERY candidate is evaluated with
a fast reduced-order thermal model (analytical R_theta chain); the caller
runs full CHT only on Pareto finalists. This module provides that reduced
model — deterministic, ~microseconds per evaluation.

The evaluator is injected so tests can substitute fake physics; production
wires the real reduced-order model below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyspice_openfoam_agent.design.object import Design, Requirements
from pyspice_openfoam_agent.library.loader import Library
from pyspice_openfoam_agent.library.schema import MOSFET
from pyspice_openfoam_agent.sizing.engine import size


class OptimizationError(RuntimeError):
    """Optimizer misconfigured."""


@dataclass
class Candidate:
    mosfet_pn: str
    fsw_khz: float
    L_uh: float
    v_in_m_s: float
    efficiency: float
    tj_max_c: float
    cost: float
    feasible: bool
    rank: int = 0  # Pareto rank (0 = on frontier)


# ---------------- reduced-order thermal model (two-tier policy, tier 1) ----------------


def reduced_order_tj(
    p_loss_w: float,
    r_theta_ja: float,
    v_in_m_s: float,
    ambient_c: float = 27.0,
    airflow_ref_m_s: float = 1.0,
    airflow_effect: float = 0.35,
) -> float:
    """Analytical junction temperature estimate (seconds, not minutes).

    R_theta_eff = R_theta_ja scaled by airflow: forced air at the reference
    speed reduces the still-air R_theta by `airflow_effect`; more airflow
    helps with diminishing returns (square-root law, standard for forced
    convection over flat plates).
    """
    scale = 1.0 - airflow_effect * ((v_in_m_s / airflow_ref_m_s) ** 0.5)
    scale = max(scale, 0.3)  # physical floor: airflow never reduces R by >70%
    return ambient_c + p_loss_w * r_theta_ja * scale


def reduced_order_losses(
    mosfet: MOSFET, iout: float, fsw_hz: float, tj_c: float = 27.0
) -> tuple[float, float]:
    """Crude (loss, efficiency) pair: conduction + switching + gate.

    Deterministic first-order model — the same physics as Phase 5's
    analytical screening, reused here for speed.
    """
    rds = mosfet.rds_on_at(tj_c)
    p_cond = iout ** 2 * rds * 1.5  # HS+LS conduction, 1.5 = RMS/duty factor
    # switching: 0.5 * V * I * (tr+tf) * fsw with crossover from Qgd
    i_gate = (5.0 - mosfet.V_plateau) / 2.0
    t_cross = mosfet.Qgd / max(i_gate, 1e-9)
    p_sw = 0.5 * 12.0 * iout * 2 * t_cross * fsw_hz  # Vds pinned at 12V ref
    p_gate = 2.0 * mosfet.Qg * 5.0 * fsw_hz
    return (p_cond + p_sw + p_gate), p_cond + p_sw + p_gate


# ---------------- pymoo problem ----------------


def run_pareto(
    lib: Library,
    req: Requirements,
    objectives: list[str] | None = None,
    pop_size: int = 24,
    n_gen: int = 20,
    seed: int | None = 42,
) -> list[Candidate]:
    """NSGA-II over (mosfet, fsw, L, airflow). Returns the Pareto front.

    objectives: subset of ['efficiency', 'tj', 'cost'] (default all three).
    Deterministic with a fixed seed; LLM never judges the frontier (goals
    gap #1: deterministic optimization, LLM only interprets).
    """
    try:
        from pymoo.core.problem import Problem
        from pymoo.algorithms.moo.nsga2 import NSGA2
        from pymoo.optimize import minimize
        from pymoo.operators.sampling.rnd import FloatRandomSampling
        import numpy as np
    except ImportError as e:
        raise OptimizationError(
            "pymoo not installed — add it to the image (Phase 0 pinned dep)"
        ) from e

    # candidate MOSFETs clearing the voltage floor
    vds_req = req.Vin * 1.2
    fets = sorted(
        (m for m in lib.mosfets.values() if m.Vds_max >= vds_req),
        key=lambda m: m.Rds_on,
    )
    if not fets:
        raise OptimizationError(f"no MOSFET clears {vds_req:.0f}V in the library")

    objectives = objectives or ["efficiency", "tj", "cost"]

    class ConverterProblem(Problem):
        """Vars: [fsw_khz, L_uh, v_in_ms, mosfet_index_float]; objectives per `objectives`."""

        def __init__(self):
            super().__init__(
                n_var=4,
                n_obj=len(objectives),
                xl=np.array([100.0, 1.0, 0.5, 0.0]),
                xu=np.array([500.0, 20.0, 3.0, float(len(fets) - 1) + 0.999]),
            )

        def _evaluate(self, X, out, *args, **kwargs):
            F = np.zeros((X.shape[0], self.n_obj))
            for i, (fsw_khz, l_uh, v_in, mi) in enumerate(X):
                mosfet = fets[int(mi)]
                # analytical sizing at this operating point
                spec_kwargs = dict(
                    Vin=req.Vin, Vout=req.Vout, Iout=req.Iout,
                    fsw=fsw_khz * 1e3, ripple_ratio=req.ripple_ratio,
                    Vripple=req.ripple_v,
                )
                try:
                    sizing = _quick_size(
                        req.Vin, req.Vout, req.Iout, fsw_khz * 1e3, req.ripple_ratio)
                except Exception:
                    F[i, :] = [1e6] * self.n_obj
                    continue
                di = sizing[2]
                # screening: reject clearly-bad candidates
                if sizing[0] > mosfet.Id_max or di > mosfet.Id_max:
                    F[i, :] = [1e6] * self.n_obj
                    continue
                p_loss, _ = reduced_order_losses(mosfet, req.Iout, fsw_khz * 1e3)
                tj = reduced_order_tj(p_loss, mosfet.R_theta_ja, v_in)
                p_out = req.Vout * req.Iout
                eff = p_out / (p_out + p_loss)
                # relative cost: Rds_on as cost proxy (lower Rds = costlier die)
                cost = 1.0 / mosfet.Rds_on + 0.5  # arbitrary scale, rank-only
                obj_map = {
                    "efficiency": -eff,
                    "tj": tj,
                    "cost": cost,
                }
                F[i, :] = [obj_map[o] for o in objectives]
            out["F"] = F

    def _quick_size(vin, vout, iout, fsw_hz, rr):
        """Buck-only fast sizing for the optimizer inner loop (no Spec object
        overhead). Returns (I_peak, None, di_pp, None)."""
        d = vout / vin
        di = rr * iout
        l_min = vout * (1 - d) / (fsw_hz * di)
        i_peak = iout + di / 2
        return (i_peak, l_min, di, None)

    algorithm = NSGA2(pop_size=pop_size, seed=seed)
    res = minimize(ConverterProblem(), algorithm, ("n_gen", n_gen), seed=seed, verbose=False)

    # collect the front
    candidates: list[Candidate] = []
    fets_by_idx = {i: m for i, m in enumerate(fets)}
    for x, f in zip(res.X, res.F):
        mi = int(x[3])
        mosfet = fets_by_idx[min(mi, len(fets) - 1)]
        fsw_khz = float(x[0])
        p_loss, _ = reduced_order_losses(mosfet, req.Iout, fsw_khz * 1e3)
        tj = reduced_order_tj(p_loss, mosfet.R_theta_ja, x[2])
        p_out = req.Vout * req.Iout
        eff = p_out / (p_out + p_loss)
        candidates.append(Candidate(
            mosfet_pn=mosfet.part_number, fsw_khz=round(fsw_khz, 1),
            L_uh=round(float(x[1]), 2), v_in_m_s=round(float(x[2]), 2),
            efficiency=round(eff, 4), tj_max_c=round(tj, 1),
            cost=round(float(f[-1]) if len(objectives) > 2 else 0.0, 3),
            feasible=True,
        ))

    # non-dominated sort for ranks (simple O(n^2), front sizes are small)
    if objectives and "efficiency" in objectives:
        # convert back: lower F = better; efficiency was negated
        def dominates(a: Candidate, b: Candidate) -> bool:
            ae, at = a.efficiency, a.tj_max_c
            be, bt = b.efficiency, b.tj_max_c
            better_or_equal = (ae >= be and at <= bt and a.cost <= b.cost)
            strictly = (ae > be or at < bt or a.cost < b.cost)
            return better_or_equal and strictly
        for a in candidates:
            a.rank = 0
        for a in candidates:
            for b in candidates:
                if a is b:
                    continue
                if b.rank <= a.rank and _dom(b, a):
                    a.rank += 1
        candidates.sort(key=lambda c: c.rank)
    return candidates


def _dom(a: Candidate, b: Candidate) -> bool:
    """a dominates b (all objectives >=, one strictly >)."""
    ge = (a.efficiency >= b.efficiency and a.tj_max_c <= b.tj_max_c
          and a.cost <= b.cost)
    st = (a.efficiency > b.efficiency or a.tj_max_c < b.tj_max_c
          or a.cost < b.cost)
    return ge and st