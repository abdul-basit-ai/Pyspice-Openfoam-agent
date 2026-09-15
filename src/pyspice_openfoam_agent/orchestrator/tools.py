"""Phase 11: agent tool layer — every Phase 2-10 capability as a callable.

The orchestrator's LLM sees these as Gemini function declarations; the agent
loop dispatches by name. Each tool returns a COMPACT structured dict (the
plan's context-size rule): numbers and statuses, never raw waveforms/logs.

Design notes:
- Tools are stateless functions over a shared `ToolContext` holding the run's
  working directories and the current best-known configuration. The graph
  layer owns the context's lifetime.
- Gemini function-calling schema is generated from the per-tool signature
  (name, description, OpenAPI-ish parameters) kept explicit below rather
  than reflected, so schemas stay reviewable and stable.
- Errors are returned as {"error": ...} dicts, NOT exceptions — the LLM
  must be able to observe and recover from tool failures (ReAct contract).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from pyspice_openfoam_agent.library.loader import Library, load_library
from pyspice_openfoam_agent.netlist.builder import build_and_write, build_netlist, write_netlist
from pyspice_openfoam_agent.netlist.selector import select_components
from pyspice_openfoam_agent.sizing.engine import Spec, size
from pyspice_openfoam_agent.spice.losses import extract_losses, efficiency_from_losses
from pyspice_openfoam_agent.spice.runner import run_transient, run_until_steady_state
from pyspice_openfoam_agent.thermal.board import AMBIENT_TEMP_C, build_board_geometry
from pyspice_openfoam_agent.thermal.case_writer import build_case, run_mesh_pipeline
from pyspice_openfoam_agent.thermal.extraction import extract_full_field
from pyspice_openfoam_agent.thermal.solver import run_cht_solve

# Default reference for the thermal chain: JESD51 ambient, 25 degC — the same
# value board.py/case_writer.py/validation.py use (the old 300 K "27 degC"
# constant disagreed with every other module by 2 K, audit fix).
AMBIENT_K = AMBIENT_TEMP_C + 273.15

# --- run_spice duty servo ---------------------------------------------------
# The rig is open-loop at a FIXED commanded duty; static drops (dead-time body
# diode clamp, DCR, Ron) pull the simulated Vout BELOW the design target (at
# 500 kHz a 76 ns dead time alone costs ~8% of the volt-seconds). A real
# converter sits in closed loop and compensates. The servo iterates the
# commanded charge window until the measured output matches the design point,
# so every reported metric (ripple, offset, losses, efficiency) is taken AT
# the design operating point, not at a sagged one.
_DUTY_SERVO_TOL = 0.005        # stop within 0.5% of target Vout
_DUTY_SERVO_MAX_PROBES = 8     # bounded, deterministic probe budget
_SERVO_TAIL_CYCLES = 8
_DUTY_TRIM_MIN, _DUTY_TRIM_MAX = 0.3, 3.0
# The ngspice shared library caps waveform output memory (~25 MB): roughly
# nodes x samples x 8 bytes. Keep every run under ~200k reported samples by
# thinning points_per_cycle as the cycle budget grows (the solver's internal
# adaptive timestep still resolves the ns-scale switch edges; only the
# reported samples thin out).
_SERVO_SAMPLE_BUDGET = 200_000


def _points_per_cycle(n_cycles: int) -> int:
    return int(min(200, max(40, _SERVO_SAMPLE_BUDGET // max(n_cycles, 1))))


def _settling_cycles(ctx: ToolContext, taus: float) -> int:
    """Cycles needed for the output tank to settle: soft-start (300*Tsw) plus
    `taus` RC time constants of the output pole (R_load*C_out). The old fixed
    460-cycle probe / 800-cycle cap were fine for a buck but far too short for
    a boost/buck_boost whose R*C tau is ~140 cycles — the servo chased a
    still-rising reading and the steady-state hunt ran out of budget."""
    try:
        tau_cycles = (ctx.spec.Vout / ctx.spec.Iout) * ctx.selected.capacitor.C * ctx.spec.fsw
    except Exception:
        tau_cycles = 100.0
    return int(min(max(300 + taus * tau_cycles, 460), 6000))


def _servo_probe_trim(ctx: ToolContext, trim: float) -> tuple[str, float]:
    """Build the netlist at `trim` and measure the tail-mean output of one
    fixed-length probe transient (no steady-state detection — candidates only)."""
    import numpy as np

    text = build_netlist(ctx.spec, ctx.sizing, ctx.selected, charge_trim=trim)
    n_cycles = _settling_cycles(ctx, taus=6.0)
    r = run_transient(text, fsw=ctx.spec.fsw, n_cycles=n_cycles,
                      points_per_cycle=_points_per_cycle(n_cycles))
    Tsw = 1.0 / ctx.spec.fsw
    mask = r.time >= r.time[-1] - _SERVO_TAIL_CYCLES * Tsw
    return text, float(np.mean(r["out"][mask]))


def _duty_servo(ctx: ToolContext) -> tuple[float, str, float]:
    """Find the charge-window trim holding the rig at the design Vout.

    Deterministic bounded search. The trim->Vout response is monotone but can
    be STEEPLY nonlinear (the 4-switch buck-boost moves between buck- and
    boost-like conversion regions; the boost's dead windows do not map onto
    the ideal duty relation), so this is deliberately RIG-DRIVEN, not
    model-driven: measure, bracket the target, then converge with secant
    steps (clamped inside the bracket, damped) with bisection as the
    geometric fallback. The analytic volt-second estimate only seeds the
    second probe.
    Returns (best_trim, netlist text at that trim, measured Vout)."""
    target = ctx.spec.Vout
    seen: dict[float, float] = {}
    best = None  # (trim, text, vout)

    # Renderable trim range: the builders raise on gate pulses narrower than
    # two edge times, so keep every servo candidate inside the range where
    # BOTH conducting windows still exist (HS width >= td + 2 edges; LS width
    # >= 2 edges). Outside it the rig is undefined, not merely imprecise.
    try:
        from pyspice_openfoam_agent.netlist.builder import _GATE_RISE_FALL, dead_time_for

        td = dead_time_for(ctx.selected.mosfet)
        Tsw = 1.0 / ctx.spec.fsw
        d_design = max(ctx.sizing.D, 1e-3)
        trim_lo = max(_DUTY_TRIM_MIN, (td + 2.0 * _GATE_RISE_FALL) / Tsw / d_design)
        trim_hi = min(_DUTY_TRIM_MAX, (1.0 - 3.0 * td / Tsw - 2.0 * _GATE_RISE_FALL / Tsw) / d_design)
    except Exception:
        trim_lo, trim_hi = _DUTY_TRIM_MIN, _DUTY_TRIM_MAX

    def clamp_trim(t: float) -> float:
        return min(max(t, trim_lo), trim_hi)

    def probe(trim: float) -> None:
        nonlocal best
        text, v = _servo_probe_trim(ctx, clamp_trim(trim))
        seen[round(clamp_trim(trim), 6)] = v
        if best is None or abs(v - target) < abs(best[2] - target):
            best = (clamp_trim(trim), text, v)

    def converged() -> bool:
        return abs(best[2] - target) <= _DUTY_SERVO_TOL * target

    def bracket_below_above() -> tuple[tuple[float, float], tuple[float, float]] | None:
        below = [(t, v) for t, v in seen.items() if v < target]
        above = [(t, v) for t, v in seen.items() if v >= target]
        if not below or not above:
            return None
        lo = max(below)            # closest below target
        hi = min(above)            # closest above target
        return lo, hi

    def refine(lo: tuple[float, float], hi: tuple[float, float]) -> None:
        """Secant inside the bracket (damped toward bisection, clamped inside);
        pure bisection when the secant stalls."""
        nonlocal best
        while len(seen) < _DUTY_SERVO_MAX_PROBES and not converged():
            (t_lo, v_lo), (t_hi, v_hi) = lo, hi
            slope = (v_hi - v_lo) / (t_hi - t_lo)
            t_sec = t_lo + (target - v_lo) / slope if abs(slope) > 1e-9 else \
                math.sqrt(t_lo * t_hi)
            t_bis = math.sqrt(t_lo * t_hi)
            t_try = 0.5 * (t_sec + t_bis)                 # damped secant
            t_try = min(max(t_try, t_lo * 1.001), t_hi * 0.999)
            if any(abs(t_try - t) < 1e-4 for t in seen):
                return                                    # bracket exhausted
            probe(t_try)
            v_try = seen[round(t_try, 6)]
            if (v_try < target) == (v_lo < target):
                lo = (t_try, v_try)
            else:
                hi = (t_try, v_try)

    probe(1.0)
    # Analytic volt-second estimate: seeds the second bracket point (its
    # accuracy varies by topology — the rig measurements are authoritative).
    start = 1.0
    try:
        from pyspice_openfoam_agent.netlist.builder import suggested_charge_trim

        start = suggested_charge_trim(ctx.spec, ctx.sizing, ctx.selected)
    except Exception:
        pass
    start = clamp_trim(start)
    if abs(start - 1.0) > 1e-3 and len(seen) < _DUTY_SERVO_MAX_PROBES:
        probe(start)

    # Walk outward geometrically until the target is bracketed (or bounds).
    anchor = min(seen.items(), key=lambda tv: abs(tv[1] - target))
    factor = 1.0
    while len(seen) < _DUTY_SERVO_MAX_PROBES and bracket_below_above() is None \
            and not converged():
        factor = 1.6 if factor == 1.0 else factor * 1.4
        direction = 1.0 if anchor[1] < target else -1.0
        t_try = clamp_trim(anchor[0] * direction * factor)
        if any(abs(t_try - t) < 1e-4 for t in seen):
            break
        probe(t_try)
        if abs(seen[round(t_try, 6)] - target) < abs(anchor[1] - target):
            anchor = (t_try, seen[round(t_try, 6)])

    bracket = bracket_below_above()
    if bracket is not None:
        refine(*bracket)

    ctx.artifacts["duty_servo"] = {
        "target_vout": target, "history": [[t, v] for t, v in sorted(seen.items())],
        "best_trim": best[0], "best_vout": best[2],
    }
    return best


@dataclass
class ToolContext:
    """Shared state for one orchestrated run: dirs + current config."""

    run_dir: Path
    library: Library
    v_in_default: float = 1.0
    spec: Spec | None = None
    sizing: object | None = None  # SizingResult once sized
    selected: object | None = None  # SelectedComponents once selected
    netlist_path: Path | None = None
    tj_limit_k: float = 423.15  # 150 degC MOSFET limit
    cht_timeout_s: int = 600
    # Phase 5 LossBreakdown cached by run_spice so run_thermal does not
    # re-simulate (in-memory only — never serialized into artifacts)
    losses: object | None = None
    # monotonic counter for fresh thermal_case_<n>/ dirs (never overwrite)
    thermal_case_counter: int = 0
    # accumulated artifacts for the Phase 12 bundle
    artifacts: dict = field(default_factory=dict)


TOOL_SCHEMAS = [
    {
        "name": "size_converter",
        "description": (
            "Validate a converter spec, classify topology (buck/boost/buck_boost), "
            "and compute minimum L and C. Call this first for any new spec. "
            "Pass topology explicitly to force a family (e.g. buck_boost for a "
            "wide-input spec the ratio alone would classify as buck/boost)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "Vin": {"type": "number", "description": "input voltage V"},
                "Vout": {"type": "number", "description": "output voltage V"},
                "Iout": {"type": "number", "description": "output current A"},
                "fsw_khz": {"type": "number", "description": "switching frequency kHz (100-500 typical)"},
                "ripple_ratio": {"type": "number", "description": "inductor ripple ratio 0.2-0.4"},
                "Vripple": {"type": "number", "description": "output ripple budget V"},
                "topology": {"type": "string", "enum": ["buck", "boost", "buck_boost"],
                             "description": "optional: force the converter family"},
            },
            "required": ["Vin", "Vout", "Iout", "fsw_khz", "Vripple"],
        },
    },
    {
        "name": "select_components",
        "description": "Pick real MOSFET/inductor/capacitor from the library with margins.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "build_netlist",
        "description": "Emit the SPICE netlist for the current selection.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_spice",
        "description": "Run the PySpice transient until periodic steady state; returns ripple/efficiency.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_thermal",
        "description": (
            "Build the JEDEC CHT case, mesh it, and solve to steady state. "
            "Returns per-device junction temperatures (K)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "v_in_m_s": {"type": "number", "description": "airflow speed m/s (default 1.0)"},
            },
        },
    },
    {
        "name": "mitigate_thermal",
        "description": (
            "Run the Phase 10 mitigation loop (airflow/component/frequency levers) "
            "until Tj meets the limit or the budget is exhausted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tj_limit_c": {"type": "number", "description": "max junction temperature degC"},
            },
        },
    },
    {
        "name": "analyze_control_loop",
        "description": (
            "Design the Type III compensator and compute phase/gain margins "
            "deterministically (python-control/scipy), grounding the loop "
            "analysis in the SELECTED L/C/ESR. Call after select_components. "
            "Stability margins are never LLM-judged."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "control_law": {"type": "string",
                                "enum": ["voltage", "peak-current"],
                                "description": "control scheme (default voltage)"},
            },
        },
    },
    {
        "name": "electro_thermal_converge",
        "description": (
            "Run the electro-thermal fixed-point loop: recompute losses at the "
            "operating Tj (Rds_on tempco from the database), get Tj back, iterate "
            "up to 5 times until dTj < 2 degC. Non-convergence is a validation "
            "failure. Call after select_components to get the self-consistent Tj."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "v_in_m_s": {"type": "number", "description": "airflow speed m/s"},
            },
        },
    },
    {
        "name": "read_design_memory",
        "description": "Look up past design outcomes for a spec signature.",
        "parameters": {
            "type": "object",
            "properties": {
                "Vin": {"type": "number"},
                "Vout": {"type": "number"},
                "Iout": {"type": "number"},
                "fsw_khz": {"type": "number"},
            },
            "required": ["Vin", "Vout", "Iout", "fsw_khz"],
        },
    },
]


def spec_signature(Vin: float, Vout: float, Iout: float, fsw_khz: float) -> str:
    """Stable signature for a spec class (Phase 11a memory key)."""
    payload = json.dumps(
        {"Vin": round(float(Vin), 3), "Vout": round(float(Vout), 3),
         "Iout": round(float(Iout), 3), "fsw_khz": round(float(fsw_khz), 1)},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------- tool implementations ----------------


def tool_size_converter(ctx: ToolContext, Vin: float, Vout: float, Iout: float,
                        fsw_khz: float, Vripple: float,
                        ripple_ratio: float | None = None,
                        topology: str | None = None) -> dict:
    kwargs = dict(Vin=Vin, Vout=Vout, Iout=Iout, fsw=fsw_khz * 1e3, Vripple=Vripple)
    if ripple_ratio is not None:
        kwargs["ripple_ratio"] = ripple_ratio
    if topology is not None:
        kwargs["topology_constraint"] = topology
    try:
        spec = Spec(**kwargs)
        result = size(spec)
    except Exception as e:
        return {"error": f"spec infeasible: {e}"}
    ctx.spec = spec
    ctx.sizing = result
    ctx.artifacts["sizing"] = {
        "topology": result.topology, "D": result.D,
        "L_min_H": result.L_min, "C_min_F": result.C_min,
        "I_peak_A": result.I_peak,
    }
    return {
        "topology": result.topology,
        "duty_cycle": round(result.D, 4),
        "L_min_uH": round(result.L_min * 1e6, 3),
        "C_min_uF": round(result.C_min * 1e6, 3),
        "I_peak_A": round(result.I_peak, 2),
        "notes": result.notes,
    }


def tool_select_components(ctx: ToolContext) -> dict:
    if ctx.spec is None or ctx.sizing is None:
        return {"error": "call size_converter first"}
    try:
        sel = select_components(ctx.library, ctx.spec, ctx.sizing)
        # Phase 7 fast screening gate (audit fix: was never wired into the
        # pipeline — doomed designs sailed through to SPICE/CHT). Rejected
        # designs are returned as a structured failure the LLM can act on.
        from pyspice_openfoam_agent.sizing.screening import screen

        verdict = screen(ctx.spec.Vin, ctx.spec.Vout, ctx.spec.Iout, ctx.spec.fsw,
                         ctx.sizing, sel.mosfet, sel.inductor, sel.capacitor)
        if verdict.rejected:
            return {"error": "screening rejected the selected parts: "
                             + "; ".join(verdict.reasons),
                    "rejected": True, "reasons": verdict.reasons}
    except Exception as e:
        return {"error": f"selection failed: {e}"}
    ctx.selected = sel
    ctx.artifacts["components"] = {
        "mosfet": sel.mosfet.part_number, "inductor": sel.inductor.part_number,
        "capacitor": sel.capacitor.part_number,
    }
    return {
        "mosfet": sel.mosfet.part_number,
        "Rds_on_mOhm": round(sel.mosfet.Rds_on * 1e3, 2),
        "inductor": sel.inductor.part_number,
        "capacitor": sel.capacitor.part_number,
        "screening_warnings": verdict.warnings,
        "notes": sel.notes,
    }


def tool_build_netlist(ctx: ToolContext, out_path: str | None = None) -> dict:
    if ctx.selected is None:
        return {"error": "call select_components first"}
    path = Path(out_path) if out_path else Path(ctx.run_dir) / "design.cir"
    try:
        written, lint = build_and_write(ctx.spec, ctx.sizing, ctx.selected, path, lint=True)
        # connectivity validation (goals Phase 5: floating nodes / invalid
        # connections — audit fix: module existed but was never run here)
        from pyspice_openfoam_agent.netlist.validate import validate_netlist

        conn = validate_netlist(written)
        if not conn.valid:
            return {"error": f"netlist connectivity invalid: floating={conn.floating_nodes} "
                             f"unconnected={conn.unconnected_devices}",
                    "netlist": str(written)}
    except Exception as e:
        return {"error": f"netlist build/lint failed: {e}"}
    ctx.netlist_path = written
    ctx.artifacts["netlist"] = str(written)
    return {"netlist": str(written), "lint_ok": lint.ok if lint else None,
            "connectivity_valid": True}


def tool_run_spice(ctx: ToolContext, max_cycles: float = 800) -> dict:
    if ctx.netlist_path is None:
        return {"error": "call build_netlist first"}
    try:
        # Duty servo: find the commanded charge window that holds the rig at
        # the design operating point (static drops would otherwise sag Vout
        # and every metric taken from the sagged run).
        trim, servo_text, servo_vout = _duty_servo(ctx)
        # Persist the servo'd netlist so the artifact on disk is exactly what
        # was simulated, then run the authoritative steady-state transient.
        # The cycle budget scales with the output tank's settling time: a
        # fixed 800-cycle cap cannot contain soft-start + settling for slow
        # (boost/buck_boost) output poles.
        budget = max(float(max_cycles), float(_settling_cycles(ctx, taus=10.0)))
        write_netlist(servo_text, ctx.netlist_path)
        run = run_until_steady_state(
            servo_text, fsw=ctx.spec.fsw, out_node="out",
            start_cycles=40, max_cycles=budget,
            points_per_cycle=_points_per_cycle(int(budget)),
            expected_level=ctx.spec.Vout,
        )
        from pyspice_openfoam_agent.spice.runner import (
            measure_output_ripple, measure_efficiency, validate_transient_health,
        )

        if not run.steady_state.converged:
            # Clean, actionable failure — the old path crashed loss extraction
            # with a cryptic TypeError on settle_time=None (audit finding).
            return {"error": f"transient did not reach steady state within "
                             f"{max_cycles:.0f} cycles — rerun with a higher "
                             f"max_cycles or investigate the design",
                    "converged": False, "cycles": run.n_cycles_run}
        ripple = measure_output_ripple(run.result, "out", ctx.spec.fsw)
        # All Phase 3 topologies (incl. the non-inverting 4-switch buck_boost)
        # have a POSITIVE output rail — "bipolar" silently disabled the
        # negative-spike guard on a topology that needs it (audit fix).
        health = validate_transient_health(
            run.result, "out", ctx.spec.Vout, ctx.spec.fsw,
            polarity="positive",
        )
        lb = extract_losses_for(ctx, run)
        ctx.losses = lb  # cached for run_thermal (no duplicate simulation)
        # Efficiency is LOSS-BASED: eta = P_out/(P_out+P_loss). The waveform
        # measure (measure_efficiency) is loss-blind -- ideal switches cannot
        # dissipate hard-switch/Coss/Crr losses, so it always reads ~98-100%,
        # which is NOT the physical efficiency. Loss-based is authoritative.
        if lb is not None and lb.losses.total > 0:
            eff = efficiency_from_losses(ctx.spec.Vout, ctx.spec.Iout, lb.losses.total)
        else:
            eff = measure_efficiency(
                run.result, vin=ctx.spec.Vin, in_current_branch="vin#branch",
                out_node="out", r_load=ctx.spec.Vout / ctx.spec.Iout, fsw=ctx.spec.fsw,
            )
    except Exception as e:
        return {"error": f"spice run failed: {e}"}
    ctx.artifacts["spice"] = {
        "converged": run.steady_state.converged, "cycles": run.n_cycles_run,
        "ripple_V": ripple, "efficiency": eff, "losses_W": lb.losses.total if lb else None,
        "health": health, "hs_switching_W": lb.losses.hs_switching if lb else None,
        "ls_switching_W": lb.losses.ls_switching if lb else None,
        "duty_trim": trim,
    }
    # visualizations for the UI (best-effort; never fail the tool on render)
    try:
        from pyspice_openfoam_agent.ui.visualize import draw_schematic, plot_waveforms

        wf = plot_waveforms(run.result, Path(ctx.run_dir) / "waveforms.png",
                            vout_target=ctx.spec.Vout)
        ctx.artifacts["waveforms_png"] = str(wf)
        if ctx.sizing and ctx.selected:
            sch = draw_schematic(
                topology=ctx.sizing.topology, mosfet_pn=ctx.selected.mosfet.part_number,
                inductor_pn=ctx.selected.inductor.part_number,
                inductor_uh=ctx.selected.inductor.L * 1e6,
                capacitor_pn=ctx.selected.capacitor.part_number,
                capacitor_uf=ctx.selected.capacitor.C * 1e6,
                out_png=Path(ctx.run_dir) / "schematic.png",
                vin=ctx.spec.Vin, vout=ctx.spec.Vout,
            )
            ctx.artifacts["schematic_png"] = str(sch)
    except Exception:
        pass
    return {
        "converged": run.steady_state.converged,
        "cycles_to_steady": run.n_cycles_run,
        "ripple_mV": round(ripple * 1e3, 2),
        "efficiency": round(eff, 4),
        "total_loss_W": round(lb.losses.total, 3) if lb else None,
        "per_device_W": {k: round(v, 3) for k, v in lb.per_device_watts.items()} if lb else {},
        "duty_trim": round(trim, 4),
        "servo_vout_V": round(servo_vout, 4),
        "health": health,
    }


def extract_losses_for(ctx: ToolContext, run):
    """Bridging helper: Phase 5 losses on the solved steady-state window."""
    from pyspice_openfoam_agent.spice.losses import extract_losses, efficiency_from_losses

    return extract_losses(
        run.result, ctx.selected.mosfet, ctx.selected.inductor.L, ctx.selected.inductor.DCR,
        vin=ctx.spec.Vin, vout=ctx.spec.Vout, fsw=ctx.spec.fsw,
        topology=ctx.sizing.topology, settle_time=run.steady_state.cycle_time,
    )


def tool_run_thermal(ctx: ToolContext, v_in_m_s: float | None = None) -> dict:
    if ctx.selected is None or ctx.sizing is None:
        return {"error": "size/select first (need losses per device)"}
    # explicit 0 m/s is a legitimate request (still air) — `if v_in_m_s`
    # silently replaced it with the 1 m/s default (audit fix)
    v_in = ctx.v_in_default if v_in_m_s is None else float(v_in_m_s)
    try:
        # Phase 5 losses on the SPICE steady state (the heat sources).
        # REUSE the run_spice losses when available (audit fix: this tool
        # read a `artifacts["_losses"]` key NOTHING ever wrote, so the guard
        # never fired and the entire transient was re-run with a different,
        # smaller cycle cap — doubling simulation cost and failing designs
        # that run_spice had already settled).
        lb = ctx.losses
        if lb is None:
            if ctx.netlist_path is None:
                return {"error": "call build_netlist + run_spice first (need losses)"}
            tran = run_until_steady_state(
                ctx.netlist_path.read_text(), fsw=ctx.spec.fsw, out_node="out",
                start_cycles=40, max_cycles=800, expected_level=ctx.spec.Vout,
            )
            if not tran.steady_state.converged:
                return {"error": "transient did not reach steady state — no valid "
                                 "loss basis for the thermal solve",
                        "converged": False}
            lb = extract_losses(
                tran.result, ctx.selected.mosfet, ctx.selected.inductor.L,
                ctx.selected.inductor.DCR,
                vin=ctx.spec.Vin, vout=ctx.spec.Vout, fsw=ctx.spec.fsw,
                topology=ctx.sizing.topology, settle_time=tran.steady_state.cycle_time,
            )
        geo = build_board_geometry(ctx.selected.mosfet)
        # Fresh case dir per solve (audit fix: every call used the same
        # thermal_case/ dir and rmtree'd the previous solve's evidence).
        case_n = ctx.artifacts.get("_thermal_case_counter", 0) + 1
        ctx.artifacts["_thermal_case_counter"] = case_n
        case_dir = Path(ctx.run_dir) / f"thermal_case_{case_n}"
        cp = build_case(
            geo,
            {"hs_mosfet": lb.per_device_watts["hs_mosfet"],
             "ls_mosfet": lb.per_device_watts["ls_mosfet"],
             "inductor": lb.per_device_watts["inductor"]},
            case_dir,
        )
        # v_in is applied HERE (BC rewrite time) — passing it to build_case
        # used to be a silent no-op and every solve ran at 1 m/s (audit fix)
        run_mesh_pipeline(cp.root, v_in_m_s=v_in)
        result = run_cht_solve(cp, end_time=100, timeout_s=ctx.cht_timeout_s)
    except Exception as e:
        return {"error": f"thermal run failed: {e}"}
    tj_c = tj_per_device_C(result)
    p_total = sum(lb.per_device_watts.values())
    if not tj_c:
        # A "validated" solve with no extracted temperatures is meaningless:
        # extraction format drift used to slip through validation as valid
        # with converged=True and an empty Tj table (audit finding).
        return {"error": "CHT solve produced no per-device temperatures "
                         "(extraction found no solid-region Min/max T lines)",
                "converged": False}
    # Phase 12 result validation (audit fix: was dead code — converged=True
    # came from returncode+NaN alone; non-NaN divergence passed as truth)
    from pyspice_openfoam_agent.thermal.validation import validate_cht_result

    vv = validate_cht_result(result.log_path, result.tj_max, p_total)
    ctx.artifacts["thermal"] = {
        "v_in_m_s": v_in, "tj_per_device_C": tj_c,
        "converged": bool(result.converged and vv.valid),
        "validation": vv.checks,
        # K — the baseline Tj the Phase 10 mitigation loop gates on
        "tj_max_k": result.tj_max,
    }
    if not vv.valid:
        return {"error": "CHT result failed validation: " + "; ".join(vv.reasons),
                "converged": False, "validation": vv.checks,
                "tj_per_device_C": tj_c}
    # thermal PNG(s) for the UI (best-effort; needs OSMesa in the image).
    # Run each in a SUBPROCESS with a hard timeout: `pv.Plotter(off_screen=True)`
    # on a headless container without DISPLAY/OSMesa BLOCKS on GL init rather
    # than raising, so a plain try/except here would deadlock the whole agent.
    # A subprocess.timeout kills the hang and lets the run proceed without the
    # images (the scalar Tj table is still the source of truth).
    try:
        import subprocess
        import sys

        # 1) multiview annotated composite
        mv_png = Path(ctx.run_dir) / "tj_multiview.png"
        subprocess.run(
            [sys.executable, "-c",
             "from pyspice_openfoam_agent.ui.thermal_viewer import render_multiview_annotated;"
             "render_multiview_annotated(%r, %r, %r)" % (str(cp.root), str(mv_png), result.tj_per_device)],
            timeout=60, capture_output=True, check=False,
        )
        if mv_png.exists() and mv_png.stat().st_size > 1000:
            ctx.artifacts["tj_multiview_png"] = str(mv_png)
        # 2) interactive 3D HTML (self-contained, opens in a new tab)
        try:
            from pyspice_openfoam_agent.ui.thermal_viewer import export_3d_html

            html_out = Path(ctx.run_dir) / "thermal_3d.html"
            export_3d_html(cp.root, html_out, tj_map=result.tj_per_device)
            if html_out.exists() and html_out.stat().st_size > 2000:
                ctx.artifacts["tj_3d_html"] = str(html_out)
        except Exception:
            pass  # interactive HTML is additive; a failure shouldn't fail the run
        # 3) single-snapshot (legacy) for the Classic tab
        png_path = Path(ctx.run_dir) / "tj_snapshot.png"
        code = (
            "from pyspice_openfoam_agent.thermal.extraction import render_temperature_png;"
            "render_temperature_png(%r, %r)"
            % (str(cp.root), str(png_path))
        )
        subprocess.run(
            [sys.executable, "-c", code],
            timeout=45, capture_output=True, check=False,
        )
        if png_path.exists():
            ctx.artifacts["tj_png"] = str(png_path)
    except subprocess.TimeoutExpired:
        pass  # headless render hung — degrade gracefully, keep the run moving
    except Exception:
        pass
    return {
        "converged": bool(result.converged and vv.valid),
        "validated": vv.valid,
        "tj_per_device_C": tj_c,
        "tj_max_C": round(result.tj_max - 273.15, 2) if result.tj_max else None,
        "relaxation_level": result.relaxation_level,
        "case_dir": str(cp.root),
    }


def tj_per_device_C(result) -> dict:
    """Per-device junction temperatures in degC (solver returns K)."""
    return {k: round(v - 273.15, 2) for k, v in result.tj_per_device.items()}


def tool_mitigate_thermal(ctx: ToolContext, tj_limit_c: float | None = None,
                          v_in_m_s: float | None = None) -> dict:
    """Phase 10 mitigation: when the baseline CHT solve violates the Tj limit,
    escalate through the ordered levers (airflow -> component reselection ->
    fsw reduction, cost order) with a bounded CHT-solve budget. Each candidate
    re-enters the REAL pipeline (sizing -> selection -> SPICE -> CHT) on a
    child context, so the reported Tj comes from the same physics as the
    baseline. Requires run_spice + run_thermal first (needs a baseline Tj)."""
    import itertools

    from pyspice_openfoam_agent.thermal.mitigation import MitigationError, run_mitigation

    if ctx.selected is None or ctx.sizing is None or ctx.spec is None:
        return {"error": "size/select first (mitigation re-enters the pipeline)"}
    baseline_tj_k = ctx.artifacts.get("thermal", {}).get("tj_max_k")
    if baseline_tj_k is None:
        return {"error": "call run_thermal first — mitigation needs a baseline CHT Tj"}
    tj_limit_k = ctx.tj_limit_k if tj_limit_c is None else float(tj_limit_c) + 273.15
    if baseline_tj_k <= tj_limit_k:
        return {
            "feasible": True, "tj_max_C": round(baseline_tj_k - 273.15, 2),
            "note": f"baseline Tj already within the {tj_limit_k - 273.15:.0f} degC "
                    "limit — no mitigation needed",
        }

    child_counter = itertools.count(1)

    def evaluate(v_in: float, mosfet, fsw: float):
        """Rebuild-and-solve one candidate through the real tool functions."""
        n = next(child_counter)
        child = ToolContext(
            run_dir=Path(ctx.run_dir) / f"mitigation_{n}",
            library=ctx.library, v_in_default=ctx.v_in_default,
        )
        try:
            spec2 = Spec(Vin=ctx.spec.Vin, Vout=ctx.spec.Vout, Iout=ctx.spec.Iout,
                         fsw=fsw, Vripple=ctx.spec.Vripple)
            sizing2 = size(spec2)
            from pyspice_openfoam_agent.netlist.selector import (
                SelectedComponents, select_capacitor, select_inductor,
            )

            sel2 = SelectedComponents(
                mosfet=mosfet,
                inductor=select_inductor(ctx.library, sizing2),
                capacitor=select_capacitor(ctx.library, spec2, sizing2)[0],
            )
            child.spec, child.sizing, child.selected = spec2, sizing2, sel2
            spice = tool_run_spice(child)
            if "error" in spice:
                raise MitigationError(f"candidate SPICE failed: {spice['error']}")
            th = tool_run_thermal(child, v_in_m_s=v_in)
            if "error" in th:
                raise MitigationError(f"candidate CHT solve failed: {th['error']}")
            if not th.get("tj_per_device_C"):
                raise MitigationError("candidate CHT solve produced no temperatures")
        except MitigationError:
            raise
        except Exception as e:
            raise MitigationError(f"candidate configuration failed: {e}") from e
        desc = (f"v_in={v_in:g} m/s, {mosfet.part_number}, "
                f"fsw={fsw / 1e3:.0f} kHz")
        return th["tj_max_C"] + 273.15, desc, {"run_dir": str(child.run_dir)}

    try:
        outcome = run_mitigation(
            ctx.library, ctx.spec, ctx.selected.mosfet,
            tj_limit_k=tj_limit_k,
            evaluate=evaluate,
            baseline_tj_k=baseline_tj_k,
            baseline_v_in=ctx.v_in_default if v_in_m_s is None else float(v_in_m_s),
        )
    except MitigationError as e:
        return {"error": f"mitigation aborted: {e}"}
    ctx.artifacts["mitigation"] = {
        "feasible": outcome.feasible, "best_tj_max_k": outcome.best_tj_max_k,
        "iterations": outcome.iterations_used,
    }
    return {
        "feasible": outcome.feasible,
        "best_tj_max_C": round(outcome.best_tj_max_k - 273.15, 2),
        "best_config": outcome.best_description,
        "iterations_used": outcome.iterations_used,
        "plateaued_lever": outcome.plateaued_lever,
        "history": [vars(h) for h in outcome.history],
        "notes": list(getattr(outcome, "notes", [])),
    }


def tool_analyze_control_loop(ctx: ToolContext, control_law: str = "voltage") -> dict:
    if ctx.selected is None or ctx.sizing is None:
        return {"error": "call select_components first (need real L/C/ESR)"}
    try:
        from pyspice_openfoam_agent.control_loop.design import analyze_control_loop
        from pyspice_openfoam_agent.design.object import Design, Requirements

        d = Design()
        d.requirements = Requirements(
            Vin=ctx.spec.Vin, Vout=ctx.spec.Vout, Iout=ctx.spec.Iout,
            fsw_khz=ctx.spec.fsw / 1e3, ripple_v=ctx.spec.Vripple)
        d.topology.name = ctx.sizing.topology
        v = analyze_control_loop(
            d, ctx.sizing,
            L=ctx.selected.inductor.L, C=ctx.selected.capacitor.C,
            ESR=ctx.selected.capacitor.ESR, control_law=control_law,
            f_sw_hz=ctx.spec.fsw)
    except Exception as e:
        return {"error": f"control-loop analysis failed: {e}"}
    ctx.artifacts["control_loop"] = {
        "passed": v.passed, "phase_margin_deg": v.phase_margin_deg,
        "gain_margin_db": v.gain_margin_db, "crossover_hz": v.crossover_hz,
        "compensator": v.compensator, "reasons": v.reasons,
    }
    return {
        "passed": v.passed,
        "phase_margin_deg": v.phase_margin_deg,
        "gain_margin_db": v.gain_margin_db,
        "crossover_kHz": round(v.crossover_hz / 1e3, 2),
        "compensator": v.compensator,
        "compensator_zeros_hz": v.compensator_zeros_hz,
        "compensator_poles_hz": v.compensator_poles_hz,
        "reasons": v.reasons,
    }


def tool_electro_thermal_converge(ctx: ToolContext, v_in_m_s: float | None = None) -> dict:
    if ctx.selected is None:
        return {"error": "call select_components first"}
    # explicit 0 m/s (still air) is legitimate — `if v_in_m_s` silently
    # replaced it with the 1 m/s default; same falsiness bug that was fixed
    # for tool_run_thermal, missed at this call site (audit fix)
    v_in = ctx.v_in_default if v_in_m_s is None else float(v_in_m_s)
    try:
        from pyspice_openfoam_agent.thermal.electro_thermal import converge_for_design
        from pyspice_openfoam_agent.design.object import Requirements

        req = Requirements(
            Vin=ctx.spec.Vin, Vout=ctx.spec.Vout, Iout=ctx.spec.Iout,
            fsw_khz=ctx.spec.fsw / 1e3, ripple_v=ctx.spec.Vripple)
        r = converge_for_design(req, ctx.selected.mosfet, v_in_m_s=v_in)
    except Exception as e:
        return {"error": f"electro-thermal convergence failed: {e}"}
    ctx.artifacts["electro_thermal"] = {
        "converged": r.converged, "final_tj_C": r.final_tj_c,
        "iterations": r.iterations, "reason": r.reason,
    }
    payload = {
        "converged": r.converged,
        "final_tj_C": r.final_tj_c,
        "iterations": r.iterations,
        "trace": r.tj_history,
        "note": r.reason,
    }
    if not r.converged:
        # Non-convergence MUST read as a tool failure to the ReAct loop — a
        # converged=False payload with no error key was dispatched as ok=True
        # and silently accepted the last (unconverged) iterate (audit fix;
        # electro_thermal.converge's own docstring requires the caller not to
        # do this).
        payload["error"] = (
            f"electro-thermal fixed point did not converge in {r.iterations} "
            f"iterations: {r.reason}"
        )
    return payload


def tool_read_design_memory(ctx: ToolContext, Vin: float, Vout: float, Iout: float,
                            fsw_khz: float) -> dict:
    from pyspice_openfoam_agent.orchestrator.memory.design_memory import DesignMemory

    mem = DesignMemory(ctx.run_dir)
    hits = mem.lookup(Vin, Vout, Iout, fsw_khz)
    return {"hits": hits, "signature": spec_signature(Vin, Vout, Iout, fsw_khz)}


# ---------------- dispatch ----------------


@dataclass
class ToolResult:
    ok: bool
    payload: dict


def dispatch(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    """Execute one tool by name. Tool failures come back as payloads with
    ok=False (the ReAct contract: the LLM observes and recovers), never as
    raises."""
    try:
        if name == "size_converter":
            payload = tool_size_converter(ctx, **args)
        elif name == "select_components":
            payload = tool_select_components(ctx)
        elif name == "build_netlist":
            payload = tool_build_netlist(ctx)
        elif name == "run_spice":
            payload = tool_run_spice(ctx)
        elif name == "run_thermal":
            payload = tool_run_thermal(ctx, **args)
        elif name == "mitigate_thermal":
            payload = tool_mitigate_thermal(ctx, **args)
        elif name == "analyze_control_loop":
            payload = tool_analyze_control_loop(ctx, **args)
        elif name == "electro_thermal_converge":
            payload = tool_electro_thermal_converge(ctx, **args)
        elif name == "read_design_memory":
            payload = tool_read_design_memory(ctx, **args)
        else:
            return ToolResult(False, {"error": f"unknown tool {name!r}"})
        return ToolResult(ok="error" not in payload, payload=payload)
    except TypeError as e:
        return ToolResult(False, {"error": f"bad arguments for {name}: {e}"})
    except Exception as e:
        return ToolResult(False, {"error": f"{name} crashed: {e}"})