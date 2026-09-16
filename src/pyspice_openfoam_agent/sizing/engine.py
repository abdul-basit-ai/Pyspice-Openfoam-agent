"""Phase 2: feasibility pre-check, topology classification, analytical sizing.

Equations (CCM, ideal switches, small-ripple approximation), per Erickson &
Maksimovic, Fundamentals of Power Electronics:

  Buck:       D = Vout/Vin                       di_pp = Vout*(1-D)/(L*fsw)
  Boost:      D = 1 - Vin/Vout                   di_pp = Vin*D/(L*fsw)
  Buck-boost: D = Vout/(Vin+Vout)                di_pp = Vin*D/(L*fsw)

Inverting the ripple equations for the MINIMUM inductance that meets a ripple
target di_pp = r*Iout (r = ripple ratio):

  Buck:       L_min = Vout*(1-D)/(fsw * r*Iout) = (Vin-Vout)*D/(fsw*r*Iout)
  Boost:      L_min = Vin*D/(fsw * r*Iout)
  Buck-boost: L_min = Vin*D/(fsw * r*Iout)

Output capacitor from charge balance (ripple dominated by capacitance term;
ESR term handled at Phase 3 part selection since it needs a real part's ESR):

  Buck:       C_min = di_pp/(8*fsw*dV_ripple)
  Boost:      C_min = Iout*D/(fsw*dV_ripple)     (all diode current charges C during D*T)
  Buck-boost: C_min = Iout*D/(fsw*dV_ripple)

These C_min forms are the standard textbook results; the boost/buck-boost
output-capacitor ripple is charge-balance on the full diode current pulse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, model_validator


class Spec(BaseModel):
    """Converter specification input — everything downstream depends on this."""

    Vin: float = Field(gt=0, description="Input voltage, V")
    Vout: float = Field(gt=0, description="Output voltage, V")
    Iout: float = Field(gt=0, description="Output current, A")
    fsw: float = Field(gt=0, description="Switching frequency, Hz")
    ripple_ratio: float = Field(
        default=0.3, gt=0, lt=1.0, description="Target inductor ripple ratio di_pp/Iout (CCM), 0.2-0.4 typical"
    )
    Vripple: float = Field(gt=0, description="Allowed output voltage ripple (peak-to-peak), V")
    # Optional constraint: restrict topology choice (e.g. design must be buck)
    topology_constraint: str | None = Field(default=None, pattern="^(buck|boost|buck_boost)$")

    @model_validator(mode="after")
    def fsw_in_project_band(self) -> "Spec":
        # Project target: 100-500 kHz silicon. Warn-band outside [50k, 1M], hard-fail outside.
        if not (50e3 <= self.fsw <= 1e6):
            raise ValueError(f"fsw={self.fsw/1e3:.0f} kHz outside supported band [50, 1000] kHz")
        return self


class FeasibilityError(ValueError):
    """Spec is physically impossible for any supported topology."""


@dataclass
class PreCheckResult:
    feasible: bool
    reasons: list[str] = field(default_factory=list)


def precheck(spec: Spec) -> PreCheckResult:
    """Reject impossible specs BEFORE burning any simulation (plan Phase 2).

    Checks are topology-independent (pure input sanity); topology-specific
    feasibility is the classifier's job.
    """
    reasons: list[str] = []
    if spec.Vin <= 0 or spec.Vout <= 0 or spec.Iout <= 0:
        reasons.append("Vin/Vout/Iout must all be positive")
    if spec.Vripple >= spec.Vout:
        reasons.append(f"Vripple ({spec.Vripple} V) must be much smaller than Vout ({spec.Vout} V)")
    if spec.ripple_ratio >= 1.0:
        reasons.append("ripple_ratio must be < 1 (CCM assumption)")
    # Power sanity: > 500 W is outside the JEDEC-board thermal scope of this project
    if spec.Vin * spec.Iout > 500.0:
        reasons.append(f"Input power {spec.Vin * spec.Iout:.0f} W exceeds 500 W project scope")
    return PreCheckResult(feasible=not reasons, reasons=reasons)


def classify_topology(spec: Spec) -> str:
    """Pick topology from the voltage ratio; respect an explicit constraint."""
    if spec.topology_constraint:
        allowed = {"buck", "boost", "buck_boost"}
        if spec.topology_constraint not in allowed:
            raise ValueError(f"topology_constraint must be one of {allowed}")
        return spec.topology_constraint
    ratio = spec.Vout / spec.Vin
    if ratio < 0.95:
        return "buck"
    if ratio > 1.05:
        return "boost"
    return "buck_boost"


@dataclass
class SizingResult:
    topology: str
    D: float  # CCM duty cycle
    L_min: float  # H, bare minimum for the ripple target
    C_min: float  # F, bare minimum for the ripple target
    di_pp: float  # A, inductor peak-to-peak ripple at L = L_min
    I_L_avg: float  # A, average inductor current
    I_peak: float  # A, peak inductor/switch current
    I_rms_sw: float  # A, RMS switch current (for MOSFET Id check)
    notes: list[str] = field(default_factory=list)


def size(spec: Spec) -> SizingResult:
    """Analytical sizing: bare minimum L and C for the spec's ripple targets."""
    pc = precheck(spec)
    if not pc.feasible:
        raise FeasibilityError("; ".join(pc.reasons))

    topo = classify_topology(spec)
    di_pp = spec.ripple_ratio * spec.Iout

    if topo == "buck":
        if spec.Vout >= spec.Vin:
            raise FeasibilityError(f"buck requires Vout < Vin (got {spec.Vout} >= {spec.Vin})")
        D = spec.Vout / spec.Vin
        L_min = spec.Vout * (1 - D) / (spec.fsw * di_pp)
        C_min = di_pp / (8 * spec.fsw * spec.Vripple)
        I_L_avg = spec.Iout
    elif topo == "boost":
        if spec.Vin >= spec.Vout:
            raise FeasibilityError(f"boost requires Vin < Vout (got {spec.Vin} >= {spec.Vout})")
        D = 1 - spec.Vin / spec.Vout
        L_min = spec.Vin * D / (spec.fsw * di_pp)
        C_min = spec.Iout * D / (spec.fsw * spec.Vripple)
        I_L_avg = spec.Iout / (1 - D)
    else:  # buck_boost (inverting magnitude; |Vout| used consistently)
        D = spec.Vout / (spec.Vin + spec.Vout)
        L_min = spec.Vin * D / (spec.fsw * di_pp)
        C_min = spec.Iout * D / (spec.fsw * spec.Vripple)
        I_L_avg = spec.Iout / (1 - D)

    I_peak = I_L_avg + di_pp / 2
    # CCM RMS switch current: conduction-only approximation I_sw_rms ~ I_L_avg*sqrt(D)
    I_rms_sw = I_L_avg * math.sqrt(D)

    notes = [f"Topology {topo}: duty {D:.3f}, ripple target {di_pp:.2f} A pp"]
    if topo == "buck" and D > 0.9:
        notes.append("Duty > 0.9 — check bootstrap gate-drive feasibility")
    if not spec.topology_constraint and 0.95 <= spec.Vout / spec.Vin <= 1.05:
        # Ambiguous band: the ratio rule picks buck_boost (the most expensive
        # option for a near-unity ratio). Surface the multi-criteria scorer's
        # opinion as an advisory note instead of leaving topology_select.py
        # dead code (audit finding) — the LLM/HITL can still override.
        try:
            from pyspice_openfoam_agent.sizing.topology_select import recommend_topology
            from pyspice_openfoam_agent.design.object import Requirements

            rec, _ = recommend_topology(Requirements(
                Vin=spec.Vin, Vout=spec.Vout, Iout=spec.Iout,
                fsw_khz=spec.fsw / 1e3, ripple_v=spec.Vripple))
            if rec != topo:
                notes.append(
                    f"near-unity ratio (band 0.95-1.05): multi-criteria scorer "
                    f"recommends {rec!r} over the default {topo!r} — override "
                    f"with an explicit topology constraint if cost/efficiency "
                    f"priorities differ")
        except Exception:
            pass  # advisory only — never block sizing on the scorer
    return SizingResult(
        topology=topo,
        D=D,
        L_min=L_min,
        C_min=C_min,
        di_pp=di_pp,
        I_L_avg=I_L_avg,
        I_peak=I_peak,
        I_rms_sw=I_rms_sw,
        notes=notes,
    )
