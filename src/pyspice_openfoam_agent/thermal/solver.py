"""Phase 8: CHT solve automation + convergence safeguards.

Runs chtMultiRegionSimpleFoam on a Phase 7 case with a wall-clock timeout and
a relaxation-factor fallback ladder: if the solver diverges (NaN residuals),
retry with more conservative under-relaxation before giving up. Per-device Tj
extraction (Phase 9's PyVista reader) is included here since the solver
output and the extraction are tightly coupled — separating them adds a file
round-trip with no benefit.

The runner uses subprocess (not foamlib's async API) for the solve itself:
foamlib's case runner is excellent for batch management, but the agent loop
needs per-run timeout + divergence detection + automatic relaxation retry,
which is cleaner as a subprocess wrapper with log parsing.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pyspice_openfoam_agent.thermal.case_writer import CasePaths

DEFAULT_TIMEOUT_S = 600
DEFAULT_END_TIME = 200

# relaxation fallback ladder: try the tutorial defaults first, then progressively
# lower under-relaxation. Each entry is (p_rgh, rho, U, h) for the air region.
_RELAXATION_LADDER = [
    (0.7, 1.0, 0.4, 0.9),   # tutorial defaults
    (0.5, 0.7, 0.3, 0.5),   # moderate
    (0.3, 0.5, 0.2, 0.3),   # conservative
]


class SolverError(RuntimeError):
    """CHT solver failed to converge after all relaxation fallbacks."""


@dataclass
class SolverResult:
    converged: bool
    iterations: int
    tj_max: float | None  # K, max temp across all solid regions
    tj_per_device: dict[str, float]  # device tag -> max T (K)
    log_path: Path
    relaxation_level: int  # which ladder rung succeeded (0 = defaults)
    notes: list[str] = field(default_factory=list)


def _set_end_time(case: Path, end_time: int) -> None:
    cd = case / "system" / "controlDict"
    t = cd.read_text()
    t = re.sub(r"endTime\s+[0-9.]+;", f"endTime         {end_time};", t)
    t = re.sub(r"stopAt\s+\w+\s*;", "stopAt          endTime;", t)
    cd.write_text(t)


def _apply_relaxation(case: Path, level: int) -> None:
    """Apply the level-th relaxation setting to the air region's fvSolution."""
    if level <= 0:
        return  # use tutorial defaults as-is
    p_rgh, rho, u, h = _RELAXATION_LADDER[min(level, len(_RELAXATION_LADDER) - 1)]
    fs = case / "system" / "air" / "fvSolution"
    if not fs.exists():
        return
    t = fs.read_text()
    t = re.sub(r"p_rgh\s+[0-9.]+;", f"p_rgh           {p_rgh};", t)
    t = re.sub(r"rho\s+[0-9.]+;", f"rho             {rho};", t)
    t = re.sub(r"U\s+[0-9.]+;", f"U               {u};", t)
    t = re.sub(r"h\s+[0-9.]+;", f"h               {h};", t)
    fs.write_text(t)


def _check_divergence(log: str) -> bool:
    """Return True if the solver log shows NaN/inf residuals (= divergence)."""
    nan_count = log.lower().count("nan")
    # some "nan" substrings appear in path names; only count in residual lines
    nan_residuals = len(re.findall(r"residual\s*=\s*nan", log, re.I))
    return nan_residuals > 3


def _extract_tj(case: Path, regions: tuple[str, ...]) -> dict[str, float]:
    """Extract per-region max T from the last time step's solver output.

    Uses the solver log's 'Min/max T' lines (printed per region per iteration)
    as a lightweight alternative to PyVista for the steady-state result — the
    log already carries the numbers we need. Phase 9's PyVista reader remains
    available for full-field extraction when the agent needs spatial data.
    """
    log = (case / "solve.log").read_text(errors="replace")
    # last occurrence of "Min/max T:<lo> <hi>" per solid region
    tj: dict[str, float] = {}
    # the log prints "Min/max T" per region in order; parse the LAST batch
    # (the final iteration). Each region block starts with "Solving for ... region <name>"
    # followed by "Min/max T:..." for that region.
    for region in regions:
        if region == "air":
            continue
        # find all "Solving for solid region <name>" followed by Min/max T
        pattern = rf"Solving for solid region {re.escape(region)}.*?Min/max T:([0-9eE.+-]+)\s+([0-9eE.+-]+)"
        matches = re.findall(pattern, log, re.S)
        if matches:
            tj[region] = float(matches[-1][1])  # max T from the last match
    return tj


def run_cht_solve(
    case: CasePaths,
    end_time: int = DEFAULT_END_TIME,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> SolverResult:
    """Run chtMultiRegionSimpleFoam with timeout + relaxation fallback.

    Returns SolverResult with Tj per device. Raises SolverError if all
    relaxation levels diverge or the solver times out at every level.
    """
    case_path = case.root
    _set_end_time(case_path, end_time)
    env = {"FOAM_SIGFPE": "0", "PATH": __import__("os").environ.get("PATH", "")}

    for level in range(len(_RELAXATION_LADDER)):
        _apply_relaxation(case_path, level)
        try:
            proc = subprocess.run(
                ["chtMultiRegionSimpleFoam"],
                cwd=str(case_path),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env={**__import__("os").environ, **env},
            )
        except subprocess.TimeoutExpired:
            # try next relaxation level
            continue

        log = proc.stdout + proc.stderr
        log_path = case_path / "solve.log"
        log_path.write_text(log)

        if proc.returncode == 0 and not _check_divergence(log):
            # success — extract Tj
            tj = _extract_tj(case_path, case.regions)
            tj_max = max(tj.values()) if tj else None
            return SolverResult(
                converged=True,
                iterations=log.count("Time = "),
                tj_max=tj_max,
                tj_per_device=tj,
                log_path=log_path,
                relaxation_level=level,
                notes=[f"converged at relaxation level {level}"],
            )
        # diverged or crashed — try next relaxation level
        continue

    raise SolverError(
        f"CHT solver failed to converge after {len(_RELAXATION_LADDER)} relaxation levels. "
        f"See {case_path / 'solve.log'}"
    )