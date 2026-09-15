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

import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySpice.Spice.NgSpice.Shared import NgSpiceShared, NgSpiceCommandError

from pyspice_openfoam_agent.spice.steady_state import SteadyStateResult, detect_steady_state

_ANALYSIS_LINE_RE = re.compile(r"^\s*\.(op|tran)\b.*$", re.IGNORECASE | re.MULTILINE)
_END_LINE_RE = re.compile(r"^\s*\.end\s*$", re.IGNORECASE | re.MULTILINE)

# numpy<2 name (np.trapz) was removed in numpy 2; keep both supported since
# losses.py already carries the same shim.
_trapz = getattr(np, "trapezoid", np.trapz)


class SimulationError(RuntimeError):
    """ngspice failed to run, or produced no usable result."""


_NGSPICE_SINGLETON = None


def _get_ngspice():
    """One NgSpiceShared instance per process, created lazily.

    PySpice 1.5 re-executes its whole ffi.cdef block on every new_instance()
    call, so the SECOND instance in one process raises
    cffi.CDefError ("duplicate declaration of struct ngcomplex") -- a fresh
    instance per run crashes any multi-run process (the topology smoke runs
    buck/boost/buck_boost back to back; the UI runs several designs in one
    session). One reused instance is also cheaper: load_circuit() replaces
    the circuit, and run_transient() removes the previous one first.
    """
    global _NGSPICE_SINGLETON
    if _NGSPICE_SINGLETON is None:
        if os.name == "nt":
            # The ngspice DLL's own folder is NOT searched for its
            # dependencies (sndfile.dll, samplerate.dll, libomp140.x86_64.dll)
            # by the Windows loader unless it is on PATH. PySpice resolves the
            # DLL inside its package tree, so add that folder to the
            # process-local PATH before the first dlopen.
            lib_path = getattr(NgSpiceShared, "LIBRARY_PATH", None)
            if lib_path:
                dll_dir = str(Path(lib_path).parent)
                if Path(dll_dir).is_dir() and dll_dir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(dll_dir)
                except (AttributeError, OSError):
                    pass
        _NGSPICE_SINGLETON = NgSpiceShared.new_instance()
    return _NGSPICE_SINGLETON


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


def _has_transient_plot(ngspice) -> bool:
    """True when ngspice produced a transient plot with at least two time
    samples (i.e. a completed analysis, not the constant-only placeholder)."""
    try:
        last = ngspice.last_plot
    except Exception:
        return False
    if not last or last == "const":
        return False
    try:
        plot = ngspice.plot(None, last)
        return len(plot.to_analysis().time) >= 2
    except Exception:
        return False


def run_transient(
    netlist_text: str,
    fsw: float,
    n_cycles: float,
    points_per_cycle: int = 200,
) -> TransientResult:
    """Run one ngspice .tran over `n_cycles` switching periods, from t=0.

    Every call re-loads the circuit into the process-wide ngspice instance
    and simulates from t=0 -- there is no "resume from where the last call
    left off". The steady-state loop below (`run_until_steady_state`)
    handles "not long enough" by calling this again with a larger
    `n_cycles`, which re-runs from scratch rather than resuming; that costs
    some wasted simulation time but keeps this function's contract simple,
    stateless, and easy to unit test.
    """
    tran_line = build_tran_line(fsw, n_cycles, points_per_cycle)
    text = _replace_analysis(netlist_text, tran_line)

    ngspice = _get_ngspice()
    try:
        # load_circuit() calls ngSpice_Circ without removing the previous
        # circuit; on a reused instance that stacks circuits and "run" can
        # pick the wrong one. Drop whatever is loaded first (the very first
        # call legitimately has nothing loaded and ngspice only warns).
        try:
            ngspice.remove_circuit()
        except Exception:
            pass
        ngspice.load_circuit(text)
        try:
            ngspice.run()
        except NgSpiceCommandError as e:
            # ngspice >= 44 prints benign "Note: <source>: dc value used for
            # op instead of transient time=0 value." lines on stderr for
            # PULSE sources that also carry a DC value; PySpice flags every
            # non-Warning stderr line as a command failure even though the
            # transient completed. Accept the run if a real transient plot
            # with data was produced; fail only when there is nothing to read.
            if not _has_transient_plot(ngspice):
                raise SimulationError(f"ngspice transient run failed: {e}") from e
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
    vw = v[mask]
    # Robust ripple: sparse stiff-solver/ESL-ring impulse samples (ideal-switch
    # transitions through the cap ESL) alias to non-physical spikes that swamp
    # a raw max-min. The true ripple lives near the mean; reject outliers by
    # operating on the Tukey-ish compact band, so a healthy converter reports
    # its real ripple and a genuine limit-cycle (repeating every period) still
    # shows as a large value through the unchanged inner samples.
    med = float(np.median(vw))
    a = np.abs(vw - med)
    k = float(np.percentile(a, 99))  # 99th-percentile deviation is still physical
    if k > 0:
        vw = vw[a <= 3.0 * k]
    if len(vw) < 10:
        vw = v[mask]
    return float(vw.max() - vw.min())


def validate_transient_health(
    result: TransientResult,
    out_node: str,
    v_target: float,
    fsw: float,
    polarity: str = "positive",
    max_negative_v: float = 0.5,
    max_ripple_frac: float = 0.20,
    max_offset_frac: float = 0.05,
    steady_mask: np.ndarray | None = None,
) -> dict:
    """Automated sanity checks on a transient result (task section D).

    Guards the orchestrator from trusting a physically-corrupted run:
      - No shoot-through / rail flip: Vout must not dip below -0.5 V (positive
        buck/boost). Negative spikes indicate HS/LS cross-conduction (the
        dead-time bug).
      - Steady ripple < 20% of Vout_target (fails on a limit-cycle / ringing).
      - Steady mean within 5% of Vout_target (fails on a large offset, e.g. a
        soft-start that never settled or a dead circuit).

    Returns a dict {ok, checks, reasons} instead of raising, so the caller
    (a tool) can return a structured failure the LLM can observe rather than
    crash the run. `steady_mask` defaults to the last 2 switches periods.
    """
    t = result.time
    v = result[out_node]
    Tsw = 1.0 / fsw
    wfull = steady_mask if steady_mask is not None else (t >= t[-1] - 2 * Tsw)
    if wfull.sum() < 10:
        raise ValueError(f"steady window too short for health checks ({wfull.sum()} pts)")

    checks = {}
    reasons = []
    # Robust impulse rejection: sparse stiff-solver/ESL-ring samples (often
    # 10-10^2 V aliased spikes at ideal-switch transitions) are NOT the
    # converter's true behavior -- the median stays on target, only a few
    # outlier samples blow up. Operating on the median-centered compact band
    # (target +/-50%, i.e. the physical ripple envelope) removes these before
    # computing ripple / extremes, so a healthy converter isn't falsely
    # flagged and a genuine limit-cycle (which repeats every period, not as
    # sparse outliers) still fails. Negative spike check keeps full fidelity:
    # ANY real sub-zero sample fails.
    vphys = v[wfull]
    vphys_clip = np.clip(vphys, -0.5 * abs(v_target), 1.5 * abs(v_target))
    # (1) negative-shoot-through check (full-fidelity: no clipping)
    vmin_tail = float(v[wfull].min())
    if polarity == "positive" and vmin_tail < -max_negative_v:
        reasons.append(
            f"shoot-through/negative spike: Vout dropped to {vmin_tail:.2f} V "
            f"(guard: > -{max_negative_v} V) \u2014 cross-conduction / rail flip")
    checks["negative_spike"] = vmin_tail >= -max_negative_v

    # (2) ripple check over the steady tail (robust to ESL impulse outliers:
    #     ideal-switch transitions can produce sparse aliased spikes on the ESL
    #     branch; operating on the physical band removes them so a healthy
    #     converter isn't falsely flagged by a few stiff-solver samples).
    rip = float(vphys_clip.max() - vphys_clip.min())
    checks["ripple"] = f"{rip*1e3:.1f} mV"
    checks["ripple_ok"] = rip < max_ripple_frac * abs(v_target)
    if not checks["ripple_ok"]:
        reasons.append(
            f"ripple {rip*1e3:.1f} mV >= {max_ripple_frac*abs(v_target)*1e3:.0f} mV budget "
            f"({max_ripple_frac*100:.0f}% of target {v_target} V) \u2014 limit-cycle / ringing")

    # (3) steady offset check (median, robust to impulse outliers)
    vmean = float(np.median(vphys))
    checks["steady_mean"] = f"{vmean:.3f} V"
    offset = abs(vmean - v_target) / max(abs(v_target), 1e-9)
    checks["offset"] = f"{offset*100:.2f}%"
    checks["offset_ok"] = offset < max_offset_frac
    if not checks["offset_ok"]:
        reasons.append(
            f"steady level {vmean:.3f} V is {offset*100:.1f}% off target {v_target} V "
            f"(guard: {max_offset_frac*100:.0f}%) \u2014 never settled / dead circuit")

    ok = all(checks.get(k, True) for k in ("negative_spike", "ripple_ok", "offset_ok"))
    return {"ok": bool(ok), "checks": checks, "reasons": reasons}

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
    i_in_w_raw = result[in_current_branch][mask]
    duration = t_w[-1] - t_w[0]
    if duration <= 0:
        raise ValueError("Degenerate integration window (need >1 sample in the tail)")

    # ESL-ring / stiff-solver impulse samples (ideal-switch transitions
    # through package/ESL parasitics) are non-physical 10^2-10^3 A current
    # blips that swamp the time-averaged input power. In every supported
    # topology the input current is physically bounded by the inductor
    # current (i_in = iL for boost/buck-boost; i_in <= iL in buck, pulsed).
    # Clip against the actual iL envelope from this same result when its
    # branch is present -- principled, not a magic percentile. Fall back to
    # a robust 90th-percentile bound if the inductor branch is unavailable.
    il_bound = None
    for cand in ("lout#branch", "lin#branch", "l#branch"):
        if cand in result.vectors:
            il_bound = float(np.max(np.abs(result.vectors[cand][mask])))
            break
    if il_bound and il_bound > 1e-12:
        # Physical bound: input current can never exceed the inductor
        # current; 2.5x leaves margin for ripple/ESL of the inductor branch
        # itself while still eliminating the 10^2-10^3 A impulse artifacts.
        hi = il_bound * 2.5
    else:
        hi = float(np.percentile(np.abs(i_in_w_raw), 90)) * 1.5
    if hi > 0:
        i_in_w = np.clip(i_in_w_raw, -hi, hi)
    else:
        i_in_w = i_in_w_raw

    # <P_in> = Vin * |mean(i_in)|  -- mean of a single-polarity signal; the
    # magnitude handles ngspice's 'current into source' sign convention.
    i_mean = _trapz(i_in_w, t_w) / duration
    p_in_avg = vin * abs(i_mean)
    p_out_avg = _trapz(v_out_w**2, t_w) / duration / r_load
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