"""Phase 2: natural-language spec parsing + missing-parameter detection.

Converts the user's natural-language specification into a structured
Requirements object (P4 Design.requirements), identifying missing
parameters and applying the goals' question policy:

  - safety-relevant missing params (OCP/OTP/UVLO) -> MUST ask (recorded as
    unresolved_safety_items)
  - material defaults (ripple, efficiency target) -> provisional
    recommendation recorded, flagged for user confirmation
  - non-critical defaults -> documented engineering assumption
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pyspice_openfoam_agent.design.object import Requirements


class SpecParseError(ValueError):
    """Specification cannot be parsed (contradictory or unparseable)."""


@dataclass
class SpecParseResult:
    requirements: Requirements
    missing_material: list[str]  # params the user should confirm (ask)
    missing_safety: list[str]  # MUST ask per goals policy
    assumptions: list[str]  # documented defaults applied
    contradictions: list[str]


_VOLT = re.compile(r"(?:vin|input(?:\s+voltage)?)\s*[=:]?\s*(\d+(?:\.\d+)?)\s*(?:to|–|—|-)\s*(\d+(?:\.\d+)?)?\s*v", re.I)
_VOUT = re.compile(r"(?:vout|output(?:\s+voltage)?)\s*[=:]?\s*(\d+(?:\.\d+)?)\s*v", re.I)
_IOUT = re.compile(r"(\d+(?:\.\d+)?)\s*a\b(?:\s+(?:output|load))?", re.I)
_FSW = re.compile(r"(\d+(?:\.\d+)?)\s*khz", re.I)
_RIPPLE_MV = re.compile(r"ripple[^.\d]*(\d+(?:\.\d+)?)\s*mv", re.I)
_EFF = re.compile(r"efficiency[^.\d]*(\d+(?:\.\d+)?)\s*%", re.I)
_TJ = re.compile(r"tj[^.\d]*(\d+(?:\.\d+)?)\s*°?c", re.I)
_OCP = re.compile(r"ocp[^.\d]*(\d+(?:\.\d+)?)\s*a", re.I)
_OTP = re.compile(r"otp[^.\d]*(\d+(?:\.\d+)?)\s*°?c", re.I)
_UVLO = re.compile(r"uvlo[^.\d]*(\d+(?:\.\d+)?)\s*v", re.I)


def parse_spec(text: str) -> SpecParseResult:
    """Parse NL spec -> Requirements + gap report. Raises on contradictions."""
    if not text or not text.strip():
        raise SpecParseError("empty specification")

    contradictions: list[str] = []
    missing_safety: list[str] = []

    vin_range = _VOLT.search(text)
    if vin_range:
        vin = float(vin_range.group(1))
        vin_max = float(vin_range.group(2)) if vin_range.group(2) else vin
    else:
        raise SpecParseError("Vin not found in specification — it is required")

    vout_m = _VOUT.search(text)
    if not vout_m:
        raise SpecParseError("Vout not found in specification — it is required")
    vout = float(vout_m.group(1))

    iout_m = _IOUT.search(text)
    if not iout_m:
        raise SpecParseError("Iout not found in specification — it is required")
    iout = float(iout_m.group(1))

    fsw_m = _FSW.search(text)
    if fsw_m:
        fsw_khz = float(fsw_m.group(1))
    else:
        fsw_khz = 500.0  # documented default: 500 kHz, our reference band
    if not fsw_m:
        pass  # recorded as assumption below

    rip_m = _RIPPLE_MV.search(text)
    if rip_m:
        ripple_v = float(rip_m.group(1)) / 1000.0
    else:
        # provisional recommendation: 1% of Vout (goals' example question)
        ripple_v = 0.01 * vout

    eff_m = _EFF.search(text)
    efficiency_target = float(eff_m.group(1)) / 100.0 if eff_m else None

    tj_m = _TJ.search(text)
    tj_max = float(tj_m.group(1)) if tj_m else 150.0

    ocp_m = _OCP.search(text)
    otp_m = _OTP.search(text)
    uvlo_m = _UVLO.search(text)

    # contradiction checks (P2's "detect contradictory constraints")
    if vout >= vin and "boost" not in text.lower() and "step-up" not in text.lower():
        contradictions.append(f"Vout {vout} >= Vin {vin} with no boost/step-up language")
    if efficiency_target and efficiency_target >= 1.0:
        contradictions.append(f"efficiency target {efficiency_target * 100}% >= 100% is impossible")
    if ripple_v >= vout:
        contradictions.append(f"ripple {ripple_v * 1e3:.0f} mV >= Vout {vout} V is nonsensical")
    if contradictions:
        raise SpecParseError("; ".join(contradictions))

    req = Requirements(
        Vin=vin, Vout=vout, Iout=iout, fsw_khz=fsw_khz,
        ripple_v=ripple_v,
        efficiency_target=efficiency_target,
        tj_max_c=tj_max,
        ocp_a=float(ocp_m.group(1)) if ocp_m else None,
        otp_c=float(otp_m.group(1)) if otp_m else None,
        uvlo_v=float(uvlo_m.group(1)) if uvlo_m else None,
    )

    # --- question policy ---
    assumptions: list[str] = []
    missing_material: list[str] = []
    if not rip_m:
        assumptions.append(f"ripple not specified: assumed 1% of Vout = {ripple_v * 1e3:.0f} mV — confirm")
        missing_material.append("ripple")
    if not eff_m:
        assumptions.append("efficiency target not specified: no target set (report achieved)")
        missing_material.append("efficiency target")
    if not fsw_m:
        assumptions.append("f_sw not specified: assumed 500 kHz — confirm")
        missing_material.append("f_sw")
    # safety-relevant: MUST ask (goals: do not assume values)
    if not ocp_m:
        missing_safety.append("OCP threshold")
    if not otp_m:
        missing_safety.append("OTP shutdown temperature")
    if not uvlo_m:
        missing_safety.append("UVLO threshold")
    req.unresolved_safety_items = list(missing_safety)

    return SpecParseResult(
        requirements=req,
        missing_material=missing_material,
        missing_safety=missing_safety,
        assumptions=assumptions,
        contradictions=[],
    )


def format_questions(result: SpecParseResult) -> str:
    """Human-readable question block per the goals' ask policy."""
    lines = []
    if result.missing_safety:
        lines.append("Safety-relevant protection settings are unspecified. "
                     "Please provide (or decline):")
        lines += [f"  - {m}" for m in result.missing_safety]
    if result.missing_material:
        lines.append("These defaults were applied — confirm or override:")
        lines += [f"  - {a}" for a in result.assumptions]
    return "\n".join(lines)