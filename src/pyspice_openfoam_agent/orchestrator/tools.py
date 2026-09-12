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
from dataclasses import dataclass, field
from pathlib import Path

from pyspice_openfoam_agent.library.loader import Library, load_library
from pyspice_openfoam_agent.netlist.builder import build_and_write
from pyspice_openfoam_agent.netlist.selector import select_components
from pyspice_openfoam_agent.sizing.engine import Spec, size
from pyspice_openfoam_agent.spice.losses import extract_losses
from pyspice_openfoam_agent.spice.runner import run_until_steady_state
from pyspice_openfoam_agent.thermal.board import build_board_geometry
from pyspice_openfoam_agent.thermal.case_writer import build_case, run_mesh_pipeline
from pyspice_openfoam_agent.thermal.extraction import extract_full_field
from pyspice_openfoam_agent.thermal.solver import run_cht_solve

AMBIENT_K = 300.0  # default reference for the thermal chain (27 degC)


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
    # accumulated artifacts for the Phase 12 bundle
    artifacts: dict = field(default_factory=dict)


TOOL_SCHEMAS = [
    {
        "name": "size_converter",
        "description": (
            "Validate a converter spec, classify topology (buck/boost/buck_boost), "
            "and compute minimum L and C. Call this first for any new spec."
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
                        fsw_khz: float, Vripple: float) -> dict:
    try:
        spec = Spec(Vin=Vin, Vout=Vout, Iout=Iout, fsw=fsw_khz * 1e3, Vripple=Vripple)
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
        "notes": sel.notes,
    }


def tool_build_netlist(ctx: ToolContext, out_path: str | None = None) -> dict:
    if ctx.selected is None:
        return {"error": "call select_components first"}
    path = Path(out_path) if out_path else Path(ctx.run_dir) / "design.cir"
    try:
        written, lint = build_and_write(ctx.spec, ctx.sizing, ctx.selected, path, lint=True)
    except Exception as e:
        return {"error": f"netlist build/lint failed: {e}"}
    ctx.netlist_path = written
    ctx.artifacts["netlist"] = str(written)
    return {"netlist": str(written), "lint_ok": lint.ok if lint else None}


def tool_run_spice(ctx: ToolContext, max_cycles: float = 800) -> dict:
    if ctx.netlist_path is None:
        return {"error": "call build_netlist first"}
    try:
        run = run_until_steady_state(
            ctx.netlist_path.read_text(), fsw=ctx.spec.fsw, out_node="out",
            start_cycles=40, max_cycles=max_cycles,
            expected_level=ctx.spec.Vout,
        )
        from pyspice_openfoam_agent.spice.runner import measure_output_ripple, measure_efficiency

        ripple = measure_output_ripple(run.result, "out", ctx.spec.fsw)
        eff = measure_efficiency(
            run.result, vin=ctx.spec.Vin, in_current_branch="vin#branch",
            out_node="out", r_load=ctx.spec.Vout / ctx.spec.Iout, fsw=ctx.spec.fsw,
        )
        lb = extract_losses_for(ctx, run)
    except Exception as e:
        return {"error": f"spice run failed: {e}"}
    ctx.artifacts["spice"] = {
        "converged": run.steady_state.converged, "cycles": run.n_cycles_run,
        "ripple_V": ripple, "efficiency": eff, "losses_W": lb.losses.total if lb else None,
    }
    return {
        "converged": run.steady_state.converged,
        "cycles_to_steady": run.n_cycles_run,
        "ripple_mV": round(ripple * 1e3, 2),
        "efficiency": round(eff, 4),
        "total_loss_W": round(lb.losses.total, 3) if lb else None,
        "per_device_W": {k: round(v, 3) for k, v in lb.per_device_watts.items()} if lb else {},
    }


def extract_losses_for(ctx: ToolContext, run):
    """Bridging helper: Phase 5 losses on the solved steady-state window."""
    from pyspice_openfoam_agent.spice.losses import extract_losses

    return extract_losses(
        run.result, ctx.selected.mosfet, ctx.selected.inductor.L, ctx.selected.inductor.DCR,
        vin=ctx.spec.Vin, vout=ctx.spec.Vout, fsw=ctx.spec.fsw,
        topology=ctx.sizing.topology, settle_time=run.steady_state.cycle_time,
    )


def tool_run_thermal(ctx: ToolContext, v_in_m_s: float | None = None) -> dict:
    if ctx.selected is None or ctx.sizing is None:
        return {"error": "size/select first (need losses per device)"}
    v_in = float(v_in_m_s) if v_in_m_s else ctx.v_in_default
    try:
        # Phase 5 losses on the SPICE steady state (the heat sources)
        from pyspice_openfoam_agent.spice.losses import extract_losses as _el
        from pyspice_openfoam_agent.spice.runner import run_transient

        tran = run_until_steady_state(
            ctx.netlist_path.read_text(), fsw=ctx.spec.fsw, out_node="out",
            start_cycles=40, max_cycles=400, expected_level=ctx.spec.Vout,
        )
        lb = extract_losses(
            tran.result, ctx.selected.mosfet, ctx.selected.inductor.L, ctx.selected.inductor.DCR,
            vin=ctx.spec.Vin, vout=ctx.spec.Vout, fsw=ctx.spec.fsw,
            topology=ctx.sizing.topology, settle_time=tran.steady_state.cycle_time,
        )
        geo = build_board_geometry(ctx.selected.mosfet)
        case_dir = Path(ctx.run_dir) / "thermal_case"
        cp = build_case(
            geo,
            {"hs_mosfet": lb.per_device_watts["hs_mosfet"],
             "ls_mosfet": lb.per_device_watts["ls_mosfet"],
             "inductor": lb.per_device_watts["inductor"]},
            case_dir, v_in_m_s=v_in,
        )
        run_mesh_pipeline(cp.root)
        result = run_cht_solve(cp, end_time=100, timeout_s=ctx.cht_timeout_s)
    except Exception as e:
        return {"error": f"thermal run failed: {e}"}
    tj_c = {k: round(v - 273.15, 2) for k, v in result.tj_per_device.items()}
    ctx.artifacts["thermal"] = {
        "v_in_m_s": v_in, "tj_per_device_C": tj_per_device_C(ctx, result),
        "converged": result.converged,
    }
    return {
        "converged": result.converged,
        "tj_per_device_C": tj_c,
        "tj_max_C": round(result.tj_max - 273.15, 2) if result.tj_max else None,
        "relaxation_level": result.relaxation_level,
        "case_dir": str(cp.root),
    }


def tj_per_device_C(ctx, result) -> dict:
    return {k: round(v - 273.15, 2) for k, v in result.tj_per_device.items()}


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
        elif name == "read_design_memory":
            payload = tool_read_design_memory(ctx, **args)
        else:
            return ToolResult(False, {"error": f"unknown tool {name!r}"})
        return ToolResult(ok="error" not in payload, payload=payload)
    except TypeError as e:
        return ToolResult(False, {"error": f"bad arguments for {name}: {e}"})
    except Exception as e:
        return ToolResult(False, {"error": f"{name} crashed: {e}"})