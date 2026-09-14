"""Phase 12: thermal result validation — is the CHT result trustworthy?

Per the goals: "A temperature number without convergence evidence should
not be treated as a validated result." This module checks the solver log
and result state for convergence evidence, residual behavior, and energy
balance sanity, and produces a validation verdict the orchestrator can act
on (including triggering the Phase 20 discrepancy gate).

Complements thermal/solver.py (which runs the solve): this module judges it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ValidationVerdict:
    valid: bool
    converged: bool
    energy_balance_ok: bool | None  # None = not computable from the log
    residual_history: list[float]  # last-iteration pressure residuals
    checks: dict[str, str] = field(default_factory=dict)  # check -> pass/fail/skip + detail
    reasons: list[str] = field(default_factory=list)


def validate_cht_result(solve_log: str | Path, tj_max_k: float | None,
                        power_in_w: float, ambient_k: float = 300.0,
                        r_eff_bounds: tuple[float, float] = (0.5, 100.0)) -> ValidationVerdict:
    """Validate one CHT solve from its log + extracted Tj.

    power_in_w: total device dissipation (Phase 5 losses).
    r_eff_bounds: plausible bounds on effective thermal resistance (K/W)
    from junction to ambient for a JEDEC-class board — outside them means
    the result is physically suspect even if numerically converged.
    """
    text = Path(solve_log).read_text(errors="replace") if Path(solve_log).exists() else (solve_log if isinstance(solve_log, str) else "")
    checks: dict[str, str] = {}
    reasons: list[str] = []

    # --- 1. convergence: no NaN residuals anywhere ---
    nan_residuals = len(re.findall(r"residual\s*=\s*nan", text, re.I))
    converged = nan_residuals == 0
    checks["nan_residuals"] = "PASS" if converged else f"FAIL ({nan_residuals} NaN residuals)"
    if not converged:
        reasons.append(f"solver log contains {nan_residuals} NaN residuals — result is invalid")

    # --- 2. residual trend: final-iteration residuals should decrease ---
    final_res = [float(m) for m in re.findall(
        r"Final residual = ([0-9.eE+-]+)", text)]
    residual_history = final_res[-5:]
    trend_ok = None
    if len(final_res) >= 4:
        # last value should be <= first value of the tail (decreasing trend)
        tail = residual_history
        finite = [v for v in tail if v == v]  # drop NaN
        trend_ok = len(finite) >= 2 and finite[-1] <= finite[0]
        checks["residual_trend"] = ("PASS" if trend_ok else
                                    "FAIL (final residuals not decreasing)")
        if trend_ok is False:
            reasons.append("residuals are not decreasing — solve may not be converging")
    else:
        checks["residual_trend"] = "SKIP (too few iterations logged)"

    # --- 3. energy balance sanity: Tj rise vs dissipated power ---
    energy_ok = None
    if tj_max_k is not None and power_in_w > 0:
        r_eff = (tj_max_k - ambient_k) / power_in_w
        lo, hi = r_eff_bounds
        energy_ok = lo <= r_eff <= hi
        checks["energy_balance"] = (
            f"{'PASS' if energy_ok else 'FAIL'} (R_eff = {r_eff:.2f} K/W, "
            f"plausible [{lo}, {hi}] K/W)"
        )
        if not energy_ok:
            reasons.append(
                f"effective thermal resistance {r_eff:.2f} K/W outside plausible "
                f"bounds [{lo}, {hi}] K/W — check heat-source magnitudes or BCs"
            )
    else:
        checks["energy_balance"] = "SKIP (Tj or power unavailable)"

    # --- 4. Tj physical bounds ---
    tj_ok = None
    if tj_max_k is not None:
        tj_ok = 250.0 < tj_max_k < 500.0  # K; outside = clearly unphysical
        checks["tj_bounds"] = "PASS" if tj_ok else f"FAIL (Tj_max {tj_max_k:.1f} K unphysical)"
        if not tj_ok:
            reasons.append(f"Tj_max {tj_max_k:.1f} K outside physical bounds [250, 500] K")

    valid = converged and (trend_ok is not False) and (energy_ok is not False) and (tj_ok is not False)
    return ValidationVerdict(
        valid=valid, converged=converged, energy_balance_ok=energy_ok,
        residual_history=residual_history, checks=checks, reasons=reasons,
    )