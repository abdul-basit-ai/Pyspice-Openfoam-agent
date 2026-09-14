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


# Voltages. A real spec says "12 V to 5 V" with NO "vin"/"vout" label, or
# "input 12-36 V, output 5 V". Handle both: labeled values are most explicit
# and win; otherwise fall back to the "A V to B V" / "A–B V" input-range form.
_LABELED = {
    "vin": re.compile(r"(?:vin|input(?:\s+voltage)?)\s*[=:]?\s*(\d+(?:\.\d+)?)", re.I),
    "vout": re.compile(r"(?:vout|output(?:\s+voltage)?)\s*[=:]?\s*(\d+(?:\.\d+)?)", re.I),
}
# Also allow the number to come BEFORE the label ("12 V input", "5 V output")
_LABELED_NUM_FIRST = {
    "vin": re.compile(r"(\d+(?:\.\d+)?)\s*V\s*(?:input|vin)\b", re.I),
    "vout": re.compile(r"(\d+(?:\.\d+)?)\s*V\s*(?:output|vout)\b", re.I),
}
# "12 V to 5 V" -> groups (12, 5). Also "12 V – 5 V", "12-5 V". The first is
# the input (range start), the second the output, when neither is labeled.
_TO_PAIR = re.compile(
    r"(\d+(?:\.\d+)?)\s*V\s*(?:to|–|—|->|--|--|-)\s*(\d+(?:\.\d+)?)\s*V", re.I)

_IOUT = re.compile(r"(\d+(?:\.\d+)?)\s*A", re.I)
_FSW = re.compile(r"(\d+(?:\.\d+)?)\s*kHz", re.I)
_RIPPLE_MV = re.compile(r"ripple[^.\d]{0,12}(\d+(?:\.\d+)?)\s*mV", re.I)
_EFF = re.compile(r"efficien[^.\d]{0,12}(\d+(?:\.\d+)?)\s*%", re.I)
_TJ = re.compile(r"tj[^.\d]{0,12}(\d+(?:\.\d+)?)\s*°?C", re.I)
_OCP = re.compile(r"ocp[^.\d]{0,12}(\d+(?:\.\d+)?)\s*A", re.I)
_OTP = re.compile(r"otp[^.\d]{0,12}(\d+(?:\.\d+)?)\s*°?C", re.I)
_UVLO = re.compile(r"uvlo[^.\d]{0,12}(\d+(?:\.\d+)?)\s*V", re.I)


def _first_num(pat: re.Pattern, text: str, fallback: re.Pattern | None = None,
               fallback_consumed: dict | None = None) -> float | None:
    m = pat.search(text)
    if m:
        return float(m.group(1))
    if fallback is not None:
        m2 = fallback.search(text)
        if m2:
            return float(m2.group(1))
    return None


def parse_spec(text: str) -> SpecParseResult:
    """Parse NL spec -> Requirements + gap report. Raises on contradictions."""
    if not text or not text.strip():
        raise SpecParseError("empty specification")

    contradictions: list[str] = []
    missing_safety: list[str] = []

    # --- voltages: labeled wins; else the "<V> to <V>" pair (in -> out) ---
    vin = _first_num(_LABELED["vin"], text, fallback=_LABELED_NUM_FIRST["vin"])
    vout = _first_num(_LABELED["vout"], text, fallback=_LABELED_NUM_FIRST["vout"])
    pair = _TO_PAIR.search(text)
    if pair:
        p_in, p_out = float(pair.group(1)), float(pair.group(2))
        # If only a labeled output is "out" and the pair gives a range whose
        # low end (or first value) is the input, resolve sensibly.
        if vin is None:
            vin = p_in
        if vout is None and vin is not None:
            # "12 V to 5 V": first is input, second is output.
            vout = p_out

    if vin is None:
        raise SpecParseError("Vin not found in specification — it is required")
    if vout is None:
        raise SpecParseError("Vout not found in specification — it is required")

    iout = _first_num(_IOUT, text)
    if iout is None:
        raise SpecParseError("Iout not found in specification — it is required")
    if iout <= 0 or vin <= 0 or vout <= 0:
        raise SpecParseError("Vin/Vout/Iout must each be positive")

    fsw_m = _FSW.search(text)
    if fsw_m:
        fsw_khz = float(fsw_m.group(1))
    else:
        fsw_khz = 500.0  # documented default

    rip_m = _RIPPLE_MV.search(text)
    ripple_v = float(rip_m.group(1)) / 1000.0 if rip_m else None
    if ripple_v is None:
        ripple_v = 0.01 * vout  # provisional: 1% of Vout

    eff_pct = _first_num(_EFF, text)
    efficiency_target = eff_pct / 100.0 if eff_pct is not None else None

    tj = _first_num(_TJ, text)
    tj_max = tj if tj is not None else 150.0

    ocp = _first_num(_OCP, text)
    otp = _first_num(_OTP, text)
    uvlo = _first_num(_UVLO, text)

    # --- contradiction checks (P2's "detect contradictory constraints") ---
    if efficiency_target is not None and efficiency_target >= 1.0:
        contradictions.append(
            f"efficiency target {efficiency_target * 100:.0f}% >= 100% is impossible")
    if ripple_v >= vout:
        contradictions.append(
            f"ripple {ripple_v * 1e3:.1f} mV >= Vout {vout} V is nonsensical")
    if pair and vin == vout and not (vin != vout):
        contradictions.append("input and output voltage cannot be equal "
                              "(that is not a converter step)")
    if contradictions:
        raise SpecParseError("; ".join(contradictions))

    req = Requirements(
        Vin=vin, Vout=vout, Iout=iout, fsw_khz=fsw_khz,
        ripple_v=ripple_v,
        efficiency_target=efficiency_target,
        tj_max_c=tj_max,
        ocp_a=ocp, otp_c=otp, uvlo_v=uvlo,
    )

    # --- question policy ---
    assumptions: list[str] = []
    missing_material: list[str] = []
    if rip_m is None:
        assumptions.append(
            f"ripple not specified: assumed 1% of Vout = {ripple_v * 1e3:.1f} mV — confirm")
        missing_material.append("ripple")
    if eff_pct is None:
        assumptions.append("efficiency target not specified: no target set (report achieved)")
        missing_material.append("efficiency target")
    if fsw_m is None:
        assumptions.append("f_sw not specified: assumed 500 kHz — confirm")
        missing_material.append("f_sw")
    # safety-relevant: MUST ask (goals: do not assume values)
    if ocp is None:
        missing_safety.append("OCP threshold")
    if otp is None:
        missing_safety.append("OTP shutdown temperature")
    if uvlo is None:
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