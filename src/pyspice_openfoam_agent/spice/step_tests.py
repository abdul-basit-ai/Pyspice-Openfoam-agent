"""Phase 6 checkpoint / Phase 8 advanced: load-step and input-step tests.

Injects a step into a copy of the Phase 3 netlist text (the builder stays
untouched — the steady-state rig must never carry a step) and measures the
output response on the REAL simulated waveform:

  - load step: a current source pulling `step_fraction * Iout` extra from
    the output node turns on at t_step (PWL, ~ns edge);
  - input step: the input source's soft-start PWL gains a `dv_frac` step at
    t_step.

Measured: pre-step level, post-step settled level, worst under/overshoot vs
the pre-step level, and settling time into a +/-2% band around the final
level.

OPEN-LOOP scope (documented honestly): the Phase 3 rig is an open-loop power
stage at the servo-trimmed duty (tool_run_spice). A step therefore measures
the PLANT response — the transient dip/overshoot and the natural (uncompensated)
recovery, including the DC droop from DCR/Ron. The compensated closed-loop
response is validated where it is designed: Phase 6's margin analysis on the
averaged model. These two views are complementary, not redundant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from pyspice_openfoam_agent.spice.runner import TransientResult, run_transient

# Builder conventions relied on here (netlist/builder.py):
#   input source:  "Vin <node> 0 DC <v> PWL(0 0 <t_ss> <v>)"
#   load:          "Rload out 0 <R>"
_VIN_LINE_RE = re.compile(
    r"^(?P<name>Vin)\s+(?P<node>\S+)\s+0\s+DC\s+(?P<dc>[\d.]+(?:[eE][+-]?\d+)?)\s+"
    r"PWL\(\s*(?P<pwl>[^)]*?)\s*\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_END_LINE_RE = re.compile(r"^\s*\.end\s*$", re.IGNORECASE | re.MULTILINE)

_SOFT_START_CYCLES = 300  # matches builder._soft_start_s (t_ss >= 300 Tsw)
_MAX_REPORTED_SAMPLES = 200_000  # ngspice shared-library output-memory cap


class StepTestError(ValueError):
    """Step test could not be set up or the waveform was unusable."""


@dataclass
class StepResponse:
    """One measured step response."""

    kind: str  # "load_step" | "input_step"
    step_time_s: float
    v_before: float  # mean output over the 5 cycles before the step, V
    v_final: float  # mean output over the last 5 cycles (post-step level), V
    undershoot_v: float  # worst dip below v_before after the step, V (>=0)
    overshoot_v: float  # worst rise above v_before after the step, V (>=0)
    settling_time_s: float | None  # time until |v - v_final| stays in band; None = never
    band_v: float
    open_loop: bool = True  # see module docstring — always true for this rig
    detail: str = ""


def _g(x: float) -> str:
    """Compact ngspice number format. 9 significant digits so a ~ns step
    edge does not collapse into the step time at 6 digits (two identical PWL
    time points are an ambiguous netlist)."""
    return f"{x:.9g}"


def inject_load_step(text: str, i_step_a: float, t_step_s: float,
                     edge_s: float = 2e-9, out_node: str = "out") -> str:
    """Insert (idempotently) a stepped load current source pulling i_step_a
    from `out_node`, starting at t_step_s with an `edge_s` rise."""
    text = re.sub(r"^\s*Istep\s+.*$\n?", "", text, flags=re.IGNORECASE | re.MULTILINE)
    line = (f"Istep {out_node} 0 PWL(0 0 {_g(t_step_s)} 0 "
            f"{_g(t_step_s + edge_s)} {_g(i_step_a)})")
    m = _END_LINE_RE.search(text)
    if m:
        return text[: m.start()] + line + "\n" + text[m.start():]
    return text + "\n" + line + "\n"


def inject_input_step(text: str, dv_frac: float, t_step_s: float,
                      edge_s: float = 2e-9) -> str:
    """Extend the input source's soft-start PWL with a dv_frac voltage step
    at t_step_s. Returns the modified netlist text."""
    m = _VIN_LINE_RE.search(text)
    if not m:
        raise StepTestError(
            "input source line not found or not in the expected "
            "'Vin <node> 0 DC <v> PWL(...)' builder format — cannot inject step"
        )
    v0 = float(m.group("dc"))
    v1 = v0 * (1.0 + dv_frac)
    pwl = m.group("pwl").strip().rstrip(",")
    new = (f"{m.group('name')} {m.group('node')} 0 DC {_g(v0)} "
           f"PWL({pwl} {_g(t_step_s)} {_g(v0)} {_g(t_step_s + edge_s)} {_g(v1)})")
    return text[: m.start()] + new + text[m.end():]


def _step_timing(fsw: float, vout: float, iout: float, c_out: float) -> tuple[float, float]:
    """(t_step, capture_window): step after soft-start + output-tank settling;
    capture several tank time constants after the step."""
    tsw = 1.0 / fsw
    r_load = vout / max(iout, 1e-9)
    tau_out = r_load * c_out
    t_step = max(
        (_SOFT_START_CYCLES + 20) * tsw,  # soft-start + margin
        20.0 * tau_out,  # output tank settled pre-step
    )
    capture = max(40.0 * tsw, 8.0 * tau_out)
    return t_step, capture


def _measure_step(result: TransientResult, kind: str, fsw: float,
                  out_node: str, t_step_s: float) -> StepResponse:
    t, v = result.time, result[out_node]
    pre = (t >= t_step_s - 5.0 / fsw) & (t < t_step_s)
    if pre.sum() < 5:
        raise StepTestError("fewer than 5 samples before the step — run too short")
    v_before = float(np.mean(v[pre]))
    post = t >= t_step_s
    if post.sum() < 10:
        raise StepTestError("fewer than 10 samples after the step — run too short")
    t_p, v_p = t[post], v[post]
    tail = t_p >= t_p[-1] - 5.0 / fsw
    v_final = float(np.mean(v_p[tail]))
    undershoot = max(0.0, v_before - float(np.min(v_p)))
    overshoot = max(0.0, float(np.max(v_p)) - v_before)
    band = 0.02 * max(abs(v_before), 1e-9)
    outside = np.abs(v_p - v_final) > band
    if not outside.any():
        settling = 0.0
    elif bool(outside[-1]):
        settling = None  # still outside the band at run end
    else:
        last_out = int(np.where(outside)[0][-1])
        settling = float(t_p[min(last_out + 1, len(t_p) - 1)] - t_step_s)
    return StepResponse(
        kind=kind, step_time_s=t_step_s, v_before=v_before, v_final=v_final,
        undershoot_v=undershoot, overshoot_v=overshoot,
        settling_time_s=settling, band_v=band,
    )


def _run(text: str, fsw: float, total_s: float) -> TransientResult:
    n_cycles = total_s * fsw
    ppc = int(max(40, min(200, _MAX_REPORTED_SAMPLES / max(n_cycles, 1))))
    return run_transient(text, fsw, n_cycles, points_per_cycle=ppc)


def run_load_step(netlist_text: str, fsw: float, vout: float, iout: float,
                  c_out: float, step_fraction: float = 0.5) -> StepResponse:
    """Step the load up by `step_fraction` * Iout and measure the response."""
    if not (0.05 <= step_fraction <= 1.0):
        raise StepTestError(f"step_fraction {step_fraction} outside [0.05, 1.0]")
    t_step, capture = _step_timing(fsw, vout, iout, c_out)
    text = inject_load_step(netlist_text, step_fraction * iout, t_step)
    result = _run(text, fsw, t_step + capture)
    return _measure_step(result, "load_step", fsw, "out", t_step)


def run_input_step(netlist_text: str, fsw: float, vout: float, iout: float,
                   c_out: float, dv_frac: float = 0.10) -> StepResponse:
    """Step the input rail by `dv_frac` (e.g. +10%) and measure the response."""
    if not (0.02 <= abs(dv_frac) <= 0.5):
        raise StepTestError(f"dv_frac {dv_frac} outside [0.02, 0.5] in magnitude")
    t_step, capture = _step_timing(fsw, vout, iout, c_out)
    text = inject_input_step(netlist_text, dv_frac, t_step)
    result = _run(text, fsw, t_step + capture)
    return _measure_step(result, "input_step", fsw, "out", t_step)
