"""Phase 4: cycle-to-cycle steady-state detection for periodic switching waveforms.

Design choice -- simple cycle-average threshold detector, upgrade path kept
open: per the phase plan, this is deliberately the *simple* default (stop
once consecutive full-cycle averages of the output agree to within a
relative threshold, default 0.1%), not the shooting-method / quasi-Newton
periodic-steady-state solver tools like PLECS use. The simple detector can
be slow, or mis-trigger on a still-settling waveform, for converters with
slow low-frequency dynamics (large output filters, light loading). The
interface below is deliberately detector-agnostic -- `(t, y, period) ->
SteadyStateResult` -- so a shooting-method implementation can be swapped in
later without any caller (runner.py's `run_until_steady_state`, or Phase 5+)
needing to change.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class SteadyStateResult:
    converged: bool
    cycle_index: int | None  # 0-based index of the first cycle judged steady, or None
    cycle_time: float | None  # start time (s) of that cycle, or None
    cycle_averages: np.ndarray  # per-cycle average of y, one entry per full cycle in the run
    final_delta: float  # relative change between the last cycle-average pair actually compared


def _cycle_averages(t: np.ndarray, y: np.ndarray, period: float) -> np.ndarray:
    """Time-weighted (trapezoidal) average of y over each full
    [t0 + k*period, t0 + (k+1)*period) window covered by the run.

    Interpolates onto exact cycle boundaries first so this is correct for
    ngspice's non-uniform, adaptive transient timestep (samples don't
    naturally land on cycle boundaries).
    """
    if len(t) < 2:
        return np.array([])
    n_full_cycles = int(np.floor((t[-1] - t[0]) / period))
    if n_full_cycles < 1:
        return np.array([])
    boundaries = t[0] + period * np.arange(n_full_cycles + 1)
    y_at_boundaries = np.interp(boundaries, t, y)
    averages = np.empty(n_full_cycles)
    for k in range(n_full_cycles):
        mask = (t > boundaries[k]) & (t < boundaries[k + 1])
        t_k = np.concatenate(([boundaries[k]], t[mask], [boundaries[k + 1]]))
        y_k = np.concatenate(([y_at_boundaries[k]], y[mask], [y_at_boundaries[k + 1]]))
        averages[k] = np.trapz(y_k, t_k) / period
    return averages


def detect_steady_state(
    t: np.ndarray,
    y: np.ndarray,
    period: float,
    threshold: float = 1e-3,
    consecutive: int = 3,
    expected_level: float | None = None,
    level_tolerance: float = 0.5,
) -> SteadyStateResult:
    """Steady-state once `consecutive` adjacent cycle-average pairs all agree
    to within `threshold` (relative). `threshold=1e-3` matches the plan's
    default (ΔV_avg < 0.1%); `consecutive=3` guards against a single
    coincidentally-flat cycle (e.g. mid-ring-down) reading as converged.

    `expected_level` guards against the degenerate "flat at zero" case: a
    dead circuit (e.g. broken gate drive) produces perfectly flat 0 V
    cycle-averages, which the pure threshold test happily reports as
    converged (0.0 delta). When `expected_level` is given, convergence
    additionally requires the final cycle average to be within
    `level_tolerance` (fraction, relative to expected_level) of it -- e.g.
    expected_level=5, level_tolerance=0.5 accepts averages in [2.5, 7.5].
    Pass expected_level=None to keep the pure flatness criterion.
    """
    averages = _cycle_averages(t, y, period)
    if len(averages) < consecutive + 1:
        return SteadyStateResult(False, None, None, averages, float("nan"))

    deltas = np.abs(np.diff(averages)) / np.maximum(np.abs(averages[1:]), 1e-30)
    for k in range(len(deltas) - consecutive + 1):
        window = deltas[k : k + consecutive]
        if np.all(window < threshold):
            cycle_index = k + consecutive  # cycle at which agreement first holds
            if expected_level is not None:
                final_avg = float(averages[cycle_index])
                if abs(final_avg - expected_level) > level_tolerance * abs(expected_level):
                    # Flat but at the wrong level: a dead/stuck output, not a
                    # settled converter. Keep scanning later windows in case
                    # the output reaches the expected level later in the run.
                    continue
            return SteadyStateResult(
                converged=True,
                cycle_index=cycle_index,
                cycle_time=float(t[0] + cycle_index * period),
                cycle_averages=averages,
                final_delta=float(window[-1]),
            )
    return SteadyStateResult(False, None, None, averages, float(deltas[-1]))