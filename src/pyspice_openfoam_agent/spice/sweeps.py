"""Phase 8 advanced capability: operating-condition sweeps.

Two mechanisms, both on REAL simulated waveforms:

  - LOAD sweep: text surgery on the (servo-trimmed) netlist — the design is
    FIXED (same parts, same trimmed duty), only `Rload out 0 <R>` is replaced
    per point. This measures the fixed design's load regulation, ripple and
    efficiency across the load range. The trimmed duty targets the NOMINAL
    load, so Vout shifts slightly off-target at other loads — that shift is
    the measurement (open-loop DC load regulation), not an error.

  - VIN sweep: needs the duty re-servoed per corner (an open-loop rig at one
    duty cannot hold Vout across Vin), so it re-enters the pipeline per point
    — see the orchestrator tool (child contexts, sizing + build + servo'd
    run_spice per corner). This module only provides the measurement helper.

Temperature / component-tolerance sweeps are DESCOPED: the rig carries no
temperature-dependent component models or tolerance sampling — stated
honestly rather than half-implemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pyspice_openfoam_agent.spice.runner import (
    measure_efficiency,
    measure_output_ripple,
    run_until_steady_state,
)

_RLOAD_RE = re.compile(
    r"^(?P<name>Rload)\s+(?P<pos>\S+)\s+(?P<neg>\S+)\s+(?P<r>[\d.]+(?:[eE][+-]?\d+)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class SweepError(ValueError):
    """Sweep could not be set up or a point failed."""


@dataclass
class SweepPoint:
    """One measured operating point."""

    setpoint: str  # e.g. "Iout=1.00 A" or "Vin=10.8 V"
    vout_mean: float | None
    ripple_pp: float | None
    efficiency: float | None
    converged: bool
    note: str = ""


def replace_load(netlist_text: str, r_load_new: float) -> str:
    """Replace the Rload value in a builder netlist (idempotent per value)."""
    m = _RLOAD_RE.search(netlist_text)
    if not m:
        raise SweepError(
            "Rload line not found or not in the expected 'Rload <n+> <n-> <R>' "
            "builder format — cannot sweep the load"
        )
    new = f"{m.group('name')} {m.group('pos')} {m.group('neg')} {r_load_new:.6g}"
    return netlist_text[: m.start()] + new + netlist_text[m.end():]


def run_load_sweep(
    netlist_text: str,
    fsw: float,
    vout_nominal: float,
    iout_nominal: float,
    vin: float,
    load_points: list[float],
    expected_level: float | None = None,
) -> list[SweepPoint]:
    """Sweep the load current over `load_points` (A) on a FIXED design.

    Each point re-runs the transient to steady state on the modified netlist
    (the extra Rload value is the only change) and measures Vout mean
    (steady-state detector's cycle-mean basis), p-p ripple, and efficiency.
    """
    out: list[SweepPoint] = []
    for iout in load_points:
        if iout <= 0:
            out.append(SweepPoint(f"Iout={iout:.2f} A", None, None, None, False,
                                  "non-positive load"))
            continue
        text = replace_load(netlist_text, vout_nominal / iout)
        try:
            run = run_until_steady_state(
                text, fsw=fsw, out_node="out", start_cycles=40, max_cycles=1200,
                expected_level=expected_level or vout_nominal,
            )
        except Exception as e:  # noqa: BLE001 — record the point, keep sweeping
            out.append(SweepPoint(f"Iout={iout:.2f} A", None, None, None, False,
                                  f"simulation failed: {e}"))
            continue
        ss = run.steady_state
        if not ss.converged:
            out.append(SweepPoint(f"Iout={iout:.2f} A", None, None, None, False,
                                  "did not reach steady state"))
            continue
        v_mean = float(ss.cycle_averages[-1]) if len(ss.cycle_averages) else None
        try:
            ripple = measure_output_ripple(run.result, "out", fsw)
            eff = measure_efficiency(
                run.result, vin, "vin#branch", "out", vout_nominal / iout, fsw,
            )
        except Exception as e:  # noqa: BLE001
            out.append(SweepPoint(
                f"Iout={iout:.2f} A", v_mean, None, None, True,
                f"measurement failed: {e}"))
            continue
        out.append(SweepPoint(f"Iout={iout:.2f} A", v_mean, float(ripple), eff, True))
    return out
