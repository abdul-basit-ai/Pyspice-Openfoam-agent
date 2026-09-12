"""Phase 10: thermal mitigation feedback loop (the decision tree the original
pipeline was missing entirely).

When Tj_max exceeds the spec limit, decide what to change and re-enter the
appropriate earlier phase. Three levers, tried in COST ORDER (per the plan):

  1. CHEAPEST — geometry-only: increase airflow velocity v_in (re-runs mesh+
     solve only, no re-synthesis). NOTE: the plan's H_fin/N_fin lever applies
     to finned heatsinks; our flat JEDEC board has no fins, so airflow is the
     geometry lever (same re-entry cost).
  2. MEDIUM — component reselection: pick lower-Rds_on MOSFETs from the
     Phase 1 library (re-runs Phase 3->5->8: selection, netlist, solve).
     Fails cleanly when the library has nothing better.
  3. MOST EXPENSIVE — frequency reduction: lower f_sw to cut switching loss
     (which dominates at 500 kHz), forcing L/C resize + full re-synthesis.

Iteration budget (user decision): 8 CHT solves max. On budget exhaustion or
lever exhaustion: report the best configuration found + an explicit
infeasible verdict + which lever plateaued (best-effort contract).

This module is deliberately solver-agnostic about HOW each lever re-enters
the pipeline: it takes callables (from the Phase 11 orchestrator's tool
bindings) that rebuild-and-solve a configuration and return
(Tj_max_K, metadata). That keeps Phase 10 pure decision logic, unit-testable
with fake evaluators — the expensive path only runs via the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyspice_openfoam_agent.library.loader import Library
from pyspice_openfoam_agent.library.schema import MOSFET
from pyspice_openfoam_agent.sizing.engine import Spec


class MitigationError(RuntimeError):
    """Mitigation loop misconfigured."""


@dataclass
class ConfigResult:
    """Outcome of evaluating one candidate configuration."""

    tj_max_k: float
    feasible: bool
    description: str  # what was changed vs the baseline
    metadata: dict = field(default_factory=dict)


@dataclass
class MitigationOutcome:
    """Phase 10's output: best config + verdict + escalation history."""

    feasible: bool
    best_tj_max_k: float
    best_description: str
    iterations_used: int
    plateaued_lever: str | None  # lever that stopped improving
    history: list[ConfigResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class MitigationPlan:
    """The ordered sequence of lever attempts for one mitigation run."""

    baseline_v_in: float
    baseline_mosfet: MOSFET
    v_in_steps: list[float]  # airflow increases to try
    alt_mosfets: list[MOSFET]
    f_sw_steps: list[float]  # frequency reductions to try
    max_solves: int = 8

LEVER_GEOMETRY = "geometry_airflow"
LEVER_COMPONENT = "component_reselect"
LEVER_FREQUENCY = "frequency_reduce"


def _candidate_mosfets(lib: Library, current: MOSFET, spec: Spec) -> list[MOSFET]:
    """Lower-Rds_on MOSFETs that still clear the spec's voltage/current floor.
    Sorted by Rds_on ascending (best first). Empty if none better exists."""
    vds_req = spec.Vin * 1.2
    id_req = sizing_peak(spec) * 1.3
    from pyspice_openfoam_agent.library.loader import query_mosfets

    hits = query_mosfets(lib, Vds_min=vds_req, Id_min=id_req, Rds_on_max=current.Rds_on)
    return [m for m in hits if m.Rds_on < current.Rds_on]


def sizing_peak(spec: Spec) -> float:
    """I_peak estimate without re-running the full sizing engine (buck)."""
    D = spec.Vout / spec.Vin
    di_pp = spec.ripple_ratio * spec.Iout
    return spec.Iout + di_pp / 2



def plan_mitigation(
    lib: Library,
    spec: Spec,
    baseline_mosfet: MOSFET,
    baseline_v_in: float = 1.0,
    baseline_fsw_ratio: float = 1.0,
    max_solves: int = 8,
) -> MitigationOutcome | None:
    """Compute the ordered lever plan. Returns None if nothing can be tried."""
    v_in_steps = [baseline_v_in * f for f in (2.0, 3.0)]
    alts = _candidate_mosfets(lib, baseline_mosfet, spec)
    f_sw_steps = [spec.fsw * r for r in (0.5, 0.25)]
    n_candidates = len(v_in_steps) + len(alts) + len(f_sw_steps)
    if n_candidates == 0:
        return None
    return MitigationPlan(
        baseline_v_in=baseline_v_in,
        baseline_mosfet=baseline_mosfet,
        v_in_steps=v_in_steps,
        alt_mosfets=alts,
        f_sw_steps=f_sw_steps,
        max_solves=max_solves,
    )


# ---------------- the mitigation loop ----------------


def run_mitigation(
    lib: Library,
    spec: Spec,
    baseline_mosfet: MOSFET,
    tj_limit_k: float,
    evaluate,  # callable(v_in, mosfet, fsw) -> (tj_max_k, description, meta)
    baseline_tj_k: float,
    baseline_v_in: float = 1.0,
    max_solves: int = 8,
) -> MitigationOutcome:
    """Escalate through the three levers in cost order until Tj passes or
    the budget/levers are exhausted.

    `evaluate(v_in, mosfet, fsw) -> (tj_max_k, description, metadata)` is the
    orchestrator's rebuild-and-solve callback: it re-runs selection -> solve
    with the given airflow, MOSFET, and switching frequency and returns the
    new steady-state Tj (plus a human-readable description and any metadata).

    Escalation stops at the first feasible config (spec met). If none is
    feasible within `max_solves`, the best config found is returned with an
    infeasible verdict and the lever that plateaued identified.

    Selection rule (audit fix): feasible configs dominate infeasible ones;
    among equal-feasibility configs, lowest Tj wins.
    """
    plan = plan_mitigation(
        lib, spec, baseline_mosfet,
        baseline_v_in=baseline_v_in, max_solves=max_solves,
    )
    if plan is None:
        return MitigationOutcome(
            feasible=baseline_tj_k <= tj_limit_k,
            best_tj_max_k=baseline_tj_k,
            best_description="baseline (nothing to try)",
            iterations_used=0, plateaued_lever=None,
            notes=["no alternative configurations available"],
        )

    history: list[ConfigResult] = []
    solves = 0
    notes: list[str] = []

    best_state = {"tj": baseline_tj_k, "feasible": baseline_tj_k <= tj_limit_k, "desc": "baseline"}

    def better2(tj: float, is_feasible: bool) -> bool:
        st = best_state
        if is_feasible and not st["feasible"]:
            return True
        if is_feasible != st["feasible"]:
            return False
        return tj < st["tj"]

    def try_config2(v_in: float, mosfet: MOSFET, fsw: float, desc: str) -> bool:
        """Run one evaluation; record; update best. True if feasible."""
        nonlocal solves
        if solves >= max_solves:
            return False
        solves += 1
        try:
            tj, meta = evaluate(v_in, mosfet, fsw)
        except Exception as e:
            history.append(ConfigResult(tj_max_k=float("nan"), feasible=False,
                                        description=desc, metadata={"error": str(e)}))
            notes.append(f"{desc}: evaluation failed ({e})")
            return False
        cr = ConfigResult(tj_max_k=float(tj), feasible=float(tj) <= tj_limit_k,
                          description=desc, metadata=dict(meta or {}))
        history.append(cr)
        if better2(float(tj), cr.feasible):
            best_state["tj"] = float(tj)
            best_state["feasible"] = cr.feasible
            best_state["desc"] = desc
        return cr.feasible

    # --- lever 1: airflow (cheapest) ---
    for f in plan.v_in_steps:
        if try_config2(f, plan.baseline_mosfet, spec.fsw, f"v_in={f:.1f} m/s"):
            break
        if solves >= max_solves:
            break

    # --- lever 2: component reselection ---
    if not best_state["feasible"]:
        if not plan.alt_mosfets:
            notes.append(
                f"component lever skipped: {plan.baseline_mosfet.part_number} is already "
                "the lowest-Rds_on part clearing the spec in the library"
            )
        for m in plan.alt_mosfets:
            if try_config2(baseline_v_in, m, spec.fsw, f"MOSFET={m.part_number}"):
                break
            if solves >= max_solves:
                break

    # --- lever 3: frequency reduction (most expensive: resizes L/C) ---
    if not best_state["feasible"]:
        for f in plan.f_sw_steps:
            if try_config2(baseline_v_in, plan.baseline_mosfet, f, f"f_sw={f/1e3:.0f} kHz"):
                break
            if solves >= max_solves:
                break

    # plateau detection: the last lever tried that produced no feasible config
    plateaued = None
    if not best_state["feasible"]:
        for lever, prefix in ((LEVER_FREQUENCY, "f_sw="), (LEVER_COMPONENT, "MOSFET="), (LEVER_GEOMETRY, "v_in=")):
            tries = [h for h in history if h.description.startswith(prefix)]
            if tries and not any(h.feasible for h in tries):
                plateaued = lever
                break

    notes.append(f"solved {solves}/{max_solves} budget")
    return MitigationOutcome(
        feasible=best_state["feasible"],
        best_tj_max_k=best_state["tj"],
        best_description=best_state["desc"],
        iterations_used=solves,
        plateaued_lever=plateaued,
        history=history,
        notes=notes,
    )