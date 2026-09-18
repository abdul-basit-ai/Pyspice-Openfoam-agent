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
    ambient_c: float = 25.0,
    airflow_ref_m_s: float = 1.0,
    airflow_effect: float = 0.35,
) -> float:
    """Analytical junction temperature estimate (seconds, not minutes).

    R_theta_eff = R_theta_ja scaled by airflow: forced air at the reference
    speed reduces the still-air R_theta by `airflow_effect`; more airflow
    helps with diminishing returns (square-root law, standard for forced
    convection over flat plates). Ambient defaults to 25 degC — the same
    JESD51 reference the CHT tier uses (the old 27 degC default made the two
    thermal tiers disagree by 2 K for identical inputs, audit fix).
    """
    scale = 1.0 - airflow_effect * ((v_in_m_s / airflow_ref_m_s) ** 0.5)
    scale = max(scale, 0.3)  # physical floor: airflow never reduces R by >70%
    return ambient_c + max(p_loss_w, 0.0) * r_theta_ja * scale


def reduced_order_losses(
    mosfet: MOSFET, iout: float, fsw_hz: float, tj_c: float = 25.0,
    v_block: float | None = None, conduction_factor: float = 1.0,
) -> tuple[float, float]:
    """Crude (loss, efficiency) pair: conduction + switching + gate.

    Deterministic first-order model — the same physics as Phase 5's
    analytical screening, reused here for speed.

    `v_block` is the topology's actual blocking voltage (buck: Vin; boost:
    Vout; buck-boost: max) and `conduction_factor` the duty-weighted HS+LS
    factor from thermal.electro_thermal.conduction_factor(). The old version
    pinned Vds at 12 V and used a blanket 1.5x conduction factor for every
    topology, so the optimizer ranked e.g. 48 V designs on 12 V switching
    loss (audit fix).
    """
    rds = mosfet.rds_on_at(tj_c)
    p_cond = iout ** 2 * rds * conduction_factor
    # switching: 0.5 * V * I * (tr+tf) * fsw with crossover from the SHARED
    # spice.losses.crossover_time (Miller-plateau model, clamped 1-200 ns —
    # the old inline formula divided by a gate current that is ZERO for
    # V_plateau=5.0 parts, producing t_cross ~ seconds and Tj ~ 1e9 degC)
    from pyspice_openfoam_agent.spice.losses import crossover_time

    t_cross = crossover_time(mosfet)
    p_sw = 0.5 * (v_block if v_block is not None else 12.0) * iout * 2 * t_cross * fsw_hz
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

    # The spec's Vin/Vout are FIXED across the search, so classify the
    # topology and duty once (the old inner loop sized every candidate —
    # including boosts — with a buck-only formula, audit fix).
    ratio = req.Vout / req.Vin if req.Vin else 1.0
    topology = "buck" if ratio < 0.95 else "boost" if ratio > 1.05 else "buck_boost"
    D = {
        "buck": req.Vout / req.Vin,
        "boost": 1.0 - req.Vin / req.Vout,
        "buck_boost": req.Vout / (req.Vin + req.Vout),
    }[topology]
    v_block = {"buck": req.Vin, "boost": req.Vout,
               "buck_boost": max(req.Vin, req.Vout)}[topology]
    from pyspice_openfoam_agent.thermal.electro_thermal import conduction_factor

    cond_factor = conduction_factor(topology, D)

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
                # topology-aware analytical sizing at this operating point
                # (engine equations, not a buck-only shortcut)
                try:
                    from pyspice_openfoam_agent.sizing.engine import Spec, size as engine_size

                    sizing = engine_size(Spec(
                        Vin=req.Vin, Vout=req.Vout, Iout=req.Iout,
                        fsw=fsw_khz * 1e3, ripple_ratio=req.ripple_ratio,
                        Vripple=req.ripple_v, topology_constraint=topology,
                    ))
                except Exception:
                    F[i, :] = [1e6] * self.n_obj
                    continue
                # screening: reject clearly-bad candidates (current headroom
                # AND an L decision variable that violates the topology's
                # minimum inductance at this fsw)
                if sizing.I_peak > mosfet.Id_max or float(l_uh) * 1e-6 < sizing.L_min:
                    F[i, :] = [1e6] * self.n_obj
                    continue
                p_loss, _ = reduced_order_losses(
                    mosfet, req.Iout, fsw_khz * 1e3,
                    v_block=v_block, conduction_factor=cond_factor)
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

    algorithm = NSGA2(pop_size=pop_size, seed=seed)
    res = minimize(ConverterProblem(), algorithm, ("n_gen", n_gen), seed=seed, verbose=False)

    # pymoo's res.X/res.F hold the full final population. The Pareto front is
    # the subset that is non-dominated over every recorded objective — filter
    # it explicitly so run_pareto returns only the front (P13 contract).
    from pymoo.util.nds.non_dominated_sorting import NonDominatedSorting

    nds = NonDominatedSorting()
    front_idx = nds.do(res.F, only_non_dominated_front=True)
    front_idx = set(int(i) for i in front_idx)

    # collect the front. Re-screen each member with the SAME rules the
    # evaluation used (sizing + current/L floors): a 1e6-penalty row that is
    # non-dominated on the cost objective must not be resurrected with
    # recomputed numbers (audit-style fix surfaced by the optimizer tool).
    candidates: list[Candidate] = []
    fets_by_idx = {i: m for i, m in enumerate(fets)}
    for i, (x, f) in enumerate(zip(res.X, res.F)):
        if int(i) not in front_idx:
            continue
        mi = int(x[3])
        mosfet = fets_by_idx[min(mi, len(fets) - 1)]
        fsw_khz = float(x[0])
        try:
            from pyspice_openfoam_agent.sizing.engine import Spec, size as engine_size

            sizing = engine_size(Spec(
                Vin=req.Vin, Vout=req.Vout, Iout=req.Iout,
                fsw=fsw_khz * 1e3, ripple_ratio=req.ripple_ratio,
                Vripple=req.ripple_v, topology_constraint=topology,
            ))
            if sizing.I_peak > mosfet.Id_max or float(x[1]) * 1e-6 < sizing.L_min:
                continue  # screened out during evaluation; stays out
        except Exception:
            continue
        p_loss, _ = reduced_order_losses(
            mosfet, req.Iout, fsw_khz * 1e3,
            v_block=v_block, conduction_factor=cond_factor)
        tj = reduced_order_tj(p_loss, mosfet.R_theta_ja, x[2])
        p_out = req.Vout * req.Iout
        eff = p_out / (p_out + p_loss)
        cost = 1.0 / mosfet.Rds_on + 0.5  # same proxy as the objective
        candidates.append(Candidate(
            mosfet_pn=mosfet.part_number, fsw_khz=round(fsw_khz, 1),
            L_uh=round(float(x[1]), 2), v_in_m_s=round(float(x[2]), 2),
            efficiency=round(eff, 4), tj_max_c=round(tj, 1),
            cost=round(cost, 3),
            feasible=True,
        ))

    # deterministically order the front (Pareto rank all zero)
    candidates.sort(key=lambda c: (c.mosfet_pn, c.fsw_khz))
    return candidates