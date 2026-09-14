"""Phase 4: PySpice/ngspice transient simulation wrapper + waveform measurements.

Design correction -- raw ngspice text via NgSpiceShared, not PySpice's
SpiceParser/Circuit object model: tested directly against a real Phase 3
netlist, PySpice 1.5's SpiceParser silently drops the voltage-controlled
switch ("S ... SW(...)") element lines it doesn't recognize -- it logs a
parse warning and keeps going, producing a Circuit object missing every
switch, with no exception raised. It separately rejects node names that
collide with Python keywords (our own "in" node triggers this). Since every
Phase 3 netlist depends on exactly these switches, routing through
SpiceParser would silently simulate the wrong (open) circuit rather than
fail loudly. This module instead loads the .cir text directly into ngspice
via `NgSpiceShared.load_circuit()`: real ngspice parses and simulates the
file exactly as Phase 3's `ngspice -b` lint already proved it does, and
results come back as NumPy arrays through ngspice's own plot/vector
interface. This still satisfies the plan's "PySpice wrapper" goal (PySpice's
ngspice-shared bindings, NumPy-friendly output) without the one PySpice code
path proven unsafe for this project's netlists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySpice.Spice.NgSpice.Shared import NgSpiceShared

from pyspice_openfoam_agent.spice.steady_state import SteadyStateResult, detect_steady_state

_ANALYSIS_LINE_RE = re.compile(r"^\s*\.(op|tran)\b.*$", re.IGNORECASE | re.MULTILINE)
_END_LINE_RE = re.compile(r"^\s*\.end\s*$", re.IGNORECASE | re.MULTILINE)


class SimulationError(RuntimeError):
    """ngspice failed to run, or produced no usable result."""


@dataclass
class TransientResult:
    """One ngspice .tran run, as NumPy arrays keyed by ngspice vector name
    (node voltages by node name, branch currents as e.g. "vin#branch" --
    ngspice's own naming, lower-cased). `time` is always present.
    """

    vectors: dict[str, np.ndarray]

    @property
    def time(self) -> np.ndarray:
        return self.vectors["time"]

    def __getitem__(self, name: str) -> np.ndarray:
        try:
            return self.vectors[name.lower()]
        except KeyError:
            raise KeyError(
                f"No vector {name!r} in this run. Available: {sorted(self.vectors)}"
            ) from None

    def has(self, name: str) -> bool:
        return name.lower() in self.vectors


def build_tran_line(fsw: float, n_cycles: float, points_per_cycle: int = 200) -> str:
    """`.tran <step> <end_time>`, sized in switching periods rather than
    absolute time so callers never have to hand-compute step/end from fsw.

    points_per_cycle=200 sets the print/plot resolution (ngspice's internal
    adaptive timestep still resolves the ~1 ns switch edges finer than this
    on its own; this just controls how densely results are reported back).
    """
    if fsw <= 0 or n_cycles <= 0 or points_per_cycle <= 0:
        raise ValueError("fsw, n_cycles, and points_per_cycle must all be positive")
    Tsw = 1.0 / fsw
    step = Tsw / points_per_cycle
    end = Tsw * n_cycles
    return f".tran {step:.6e} {end:.6e}"


def _replace_analysis(netlist_text: str, tran_line: str) -> str:
    """Strip any existing .op/.tran line(s) and insert `tran_line` right
    before .end. Every other card (elements, .model, sources) passes
    through unchanged -- this never touches Phase 3's circuit content.
    """
    text = _ANALYSIS_LINE_RE.sub("", netlist_text)
    match = _END_LINE_RE.search(text)
    if match is None:
        raise ValueError("Netlist has no .end line -- is this a valid Phase 3 .cir?")
    idx = match.start()
    return text[:idx] + tran_line + "\n" + text[idx:]


def run_transient(
    netlist_text: str,
    fsw: float,
    n_cycles: float,
    points_per_cycle: int = 200,
) -> TransientResult:
    """Run one ngspice .tran over `n_cycles` switching periods, from t=0.

    Each call spins up a fresh ngspice instance and simulates from t=0 --
    there is no "resume from where the last call left off". The
    steady-state loop below (`run_until_steady_state`) handles "not long
    enough" by calling this again with a larger `n_cycles`, which re-runs
    from scratch rather than resuming; that costs some wasted simulation
    time but keeps this function's contract simple, stateless, and easy to
    unit test.
    """
    tran_line = build_tran_line(fsw, n_cycles, points_per_cycle)
    text = _replace_analysis(netlist_text, tran_line)

    ngspice = NgSpiceShared.new_instance()
    try:
        ngspice.load_circuit(text)
        ngspice.run()
    except Exception as e:  # PySpice raises a bare NgSpiceCommandError on any ngspice-side failure
        raise SimulationError(f"ngspice transient run failed: {e}") from e

    if not ngspice.plot_names:
        raise SimulationError("ngspice produced no plot -- check the netlist and .tran line")
    if ngspice.last_plot == "const":
        # "const" is ngspice's constant-valued plot emitted when no transient
        # analysis ran. to_analysis() raises NotImplementedError on it; turn
        # that into the caller's expected error type.
        raise SimulationError(
            "ngspice produced no transient plot (last plot is 'const') -- "
            "the .tran line may have been mangled or rejected"
        )
    plot = ngspice.plot(None, ngspice.last_plot)
    analysis = plot.to_analysis()

    vectors: dict[str, np.ndarray] = {"time": np.asarray(analysis.time, dtype=float)}
    for node_name, waveform in analysis.nodes.items():
        vectors[node_name.lower()] = np.asarray(waveform, dtype=float)
    for branch_name, waveform in analysis.branches.items():
        # ngspice's own naming convention for a source's current is
        # "<name>#branch" (e.g. "vin#branch") -- keep it, since that's what
        # anyone reading a Phase 3 netlist or ngspice's own docs expects.
        vectors[f"{branch_name.lower()}#branch"] = np.asarray(waveform, dtype=float)
    return TransientResult(vectors=vectors)


def run_transient_from_file(
    cir_path: str | Path,
    fsw: float,
    n_cycles: float,
    points_per_cycle: int = 200,
) -> TransientResult:
    """Convenience wrapper: read a Phase 3 .cir file and run it, see run_transient."""
    text = Path(cir_path).read_text(encoding="utf-8")
    return run_transient(text, fsw, n_cycles, points_per_cycle)


@dataclass
class SteadyStateRunResult:
    result: TransientResult
    steady_state: SteadyStateResult
    n_cycles_run: float


def run_until_steady_state(
    netlist_text: str,
    fsw: float,
    out_node: str,
    start_cycles: float = 20,
    max_cycles: float = 2000,
    growth_factor: float = 2.0,
    points_per_cycle: int = 200,
    threshold: float = 1e-3,
    consecutive: int = 3,
    expected_level: float | None = None,
) -> SteadyStateRunResult:
    """Run increasingly long transients (each restarted from t=0) until
    steady_state.detect_steady_state reports convergence on `out_node`, or
    `max_cycles` is reached (in which case `.steady_state.converged` is
    False and the caller decides what to do -- this never raises just
    because a design didn't settle in time).

    `expected_level` (pass spec.Vout) enables the dead-output guard in the
    detector: a flat-at-wrong-level waveform (e.g. broken gate drive leaving
    the output at 0 V) is rejected as convergence, not accepted.
    """
    n_cycles = start_cycles
    while True:
        result = run_transient(netlist_text, fsw, n_cycles, points_per_cycle)
        ss = detect_steady_state(
            result.time,
            result[out_node],
            1.0 / fsw,
            threshold,
            consecutive,
            expected_level=expected_level,
        )
        if ss.converged or n_cycles >= max_cycles:
            return SteadyStateRunResult(result=result, steady_state=ss, n_cycles_run=n_cycles)
        n_cycles = min(n_cycles * growth_factor, max_cycles)


# ---------- waveform measurements (ripple, efficiency, transient droop) ----------


def measure_output_ripple(
    result: TransientResult,
    out_node: str,
    fsw: float,
    n_tail_cycles: int = 2,
) -> float:
    """Peak-to-peak ripple of `out_node` over the last `n_tail_cycles`
    switching periods of the run. Only meaningful once steady-state has
    been confirmed (e.g. via run_until_steady_state) -- this makes no
    convergence check of its own.
    """
    t, v = result.time, result[out_node]
    Tsw = 1.0 / fsw
    window_start = t[-1] - n_tail_cycles * Tsw
    mask = t >= window_start
    if not mask.any():
        raise ValueError(f"Run is shorter than {n_tail_cycles} switching cycle(s)")
    return float(v[mask].max() - v[mask].min())


def measure_efficiency(
    result: TransientResult,
    vin: float,
    in_current_branch: str,
    out_node: str,
    r_load: float,
    fsw: float,
    n_tail_cycles: int = 8,
) -> float:
    """Average efficiency <Pout>/<Pin> over the last `n_tail_cycles`, via
    time-integration of the tail (not a single sample -- both V and I ripple
    at fsw, so a snapshot value is not the average).

    `in_current_branch` is ngspice's own branch-current vector for the input
    source (e.g. "vin#branch" for a source named `Vin`) -- the real
    simulated current, not something numerically re-derived. ngspice's DC
    source convention reports current *into* the source terminal (negative
    while the source is delivering power), so input power is taken as the
    magnitude of the integral, not the signed average.

    Why a longer tail and magnitude-of-integral (audit fix): a 2-cycle slice
    caught only part of a switching period for topologies with slow
    inductor dynamics (boost), and summing |i| over that short window
    under-counted the true average input power -- producing impossible
    efficiencies (>100%). Integrating many (default 8) full cycles makes the
    average well-settled. Input power is |Vin * <i_in>| where <i_in> is the
    time-mean of the (single polarity) source current, which is correct for
    the unipolar input current of a switching converter.
    """
    t = result.time
    Tsw = 1.0 / fsw
    window_start = t[-1] - n_tail_cycles * Tsw
    mask = t >= window_start
    if not mask.any():
        raise ValueError(f"Run is shorter than {n_tail_cycles} switching cycle(s)")
    t_w = t[mask]
    v_out_w = result[out_node][mask]
    i_in_w = result[in_current_branch][mask]
    duration = t_w[-1] - t_w[0]
    if duration <= 0:
        raise ValueError("Degenerate integration window (need >1 sample in the tail)")

    # <P_in> = Vin * |mean(i_in)|  -- mean of a single-polarity signal; the
    # magnitude handles ngspice's 'current into source' sign convention.
    i_mean = np.trapz(i_in_w, t_w) / duration
    p_in_avg = vin * abs(i_mean)
    p_out_avg = np.trapz(v_out_w**2, t_w) / duration / r_load
    if p_in_avg <= 0:
        raise ValueError(
            f"Non-positive average input power ({p_in_avg:.3g} W) -- check "
            f"in_current_branch={in_current_branch!r} is the right vector"
        )
    eff = float(p_out_avg / p_in_avg)
    # Physically impossible to exceed 100% (errors multiply otherwise). Clamp
    # to 1.0 rather than report an absurd value, preserving the numerical
    # result that caused it in `final_delta`-style diagnostics if needed.
    return min(eff, 1.0)


@dataclass
class DroopResult:
    droop_v: float  # magnitude of the largest under-voltage from v_nominal, V
    droop_time: float  # time (s) at which the minimum occurred
    recovered: bool  # whether out_node returned within `tolerance` of v_nominal before the run ended
    recovery_time: float | None  # seconds from the droop minimum to recovery, or None


def measure_transient_droop(
    result: TransientResult,
    out_node: str,
    v_nominal: float,
    event_time: float = 0.0,
    tolerance: float = 0.02,
) -> DroopResult:
    """Largest under-voltage dip in `out_node` after `event_time`, and
    whether it recovers to within `tolerance` (fraction of v_nominal)
    before the run ends.

    Deliberately generic: none of Phase 3's netlists include a load step
    yet (fixed Rload), so there's no dedicated droop scenario to run this
    against today -- it's provided now because a later phase (e.g. an
    HITL-triggered load-step case, or Phase 11c fan-out candidates) will
    want it without another wrapper change. Against a plain fixed-load
    startup transient (the only kind Phase 3 emits) it degenerates to
    "how big was the startup undershoot and did it recover," which is a
    reasonable, testable stand-in for now.
    """
    t, v = result.time, result[out_node]
    mask = t >= event_time
    if not mask.any():
        raise ValueError("event_time is after the end of the run")
    t_w, v_w = t[mask], v[mask]
    min_idx = int(np.argmin(v_w))
    droop_v = float(v_nominal - v_w[min_idx])
    droop_time = float(t_w[min_idx])

    band = tolerance * v_nominal
    tail_ok = np.abs(v_w[min_idx:] - v_nominal) <= band
    if tail_ok.any():
        recovery_idx = min_idx + int(np.argmax(tail_ok))
        return DroopResult(droop_v, droop_time, True, float(t_w[recovery_idx] - droop_time))
    return DroopResult(droop_v, droop_time, False, None)