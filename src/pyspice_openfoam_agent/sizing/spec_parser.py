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
# INPUT RANGE, labeled either side: "Vin = 36-60 V", "input 12 to 36 V",
# "12-36 V input". The unit sits after the SECOND number only — the classic
# range form that _TO_PAIR (which requires V after both) never matched, so
# "12-36 V" silently collapsed to ONE endpoint depending on phrasing.
_RANGE_SEP = r"(?:to|–|—|-)"
_IN_RANGE_LABELED = re.compile(
    rf"(?:vin|input(?:\s+voltage)?)\s*[=:]?\s*(\d+(?:\.\d+)?)\s*{_RANGE_SEP}\s*(\d+(?:\.\d+)?)\s*V", re.I)
_IN_RANGE_NUM_FIRST = re.compile(
    rf"(\d+(?:\.\d+)?)\s*{_RANGE_SEP}\s*(\d+(?:\.\d+)?)\s*V\s*(?:input|vin)\b", re.I)
# "12 V to 5 V" -> groups (12, 5). Also "12 V – 5 V". The first is the input,
# the second the output, when neither is labeled.
_TO_PAIR = re.compile(
    r"(\d+(?:\.\d+)?)\s*V\s*(?:to|–|—|-)\s*(\d+(?:\.\d+)?)\s*V", re.I)

_IOUT = re.compile(r"(\d+(?:\.\d+)?)\s*A", re.I)
# fsw units: MHz / kHz / plain Hz ("2 MHz", "300 kHz", "500000 Hz")
_FSW_MHZ = re.compile(r"(\d+(?:\.\d+)?)\s*MHz", re.I)
_FSW_KHZ = re.compile(r"(\d+(?:\.\d+)?)\s*kHz", re.I)
_FSW_HZ = re.compile(r"(\d+(?:\.\d+)?)\s*Hz", re.I)
# ripple: mV wins, V accepted as a unit too ("ripple < 0.2 V")
_RIPPLE_MV = re.compile(r"ripple[^.\d]{0,12}(\d+(?:\.\d+)?)\s*mV", re.I)
_RIPPLE_V = re.compile(r"ripple[^.\d]{0,12}(\d+(?:\.\d+)?)\s*V", re.I)
# efficiency: keyword-first (widened gap: "should exceed " is 15 chars) and
# number-first ("95% efficiency")
_EFF = re.compile(r"efficien[a-z]*[^.\d]{0,24}(\d+(?:\.\d+)?)\s*%", re.I)
_EFF_NUM_FIRST = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:efficien)", re.I)
_TJ = re.compile(r"tj[^.\d]{0,12}(\d+(?:\.\d+)?)\s*°?C", re.I)
_TJ_WORDS = re.compile(r"junction\s+temp[a-z]*[^.\d]{0,12}(\d+(?:\.\d+)?)\s*°?C", re.I)
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

    # --- voltages. Input RANGE first ("Vin = 36-60 V", "12-36 V input"):
    # record BOTH endpoints and design at the worst-case voltage-stress
    # endpoint with a loud assumption (never silently collapse a range).
    vin_min = vin_max = None
    in_range = _IN_RANGE_LABELED.search(text) or _IN_RANGE_NUM_FIRST.search(text)
    if in_range:
        a, b = float(in_range.group(1)), float(in_range.group(2))
        vin_min, vin_max = min(a, b), max(a, b)
        vin = vin_max
        vout = _first_num(_LABELED["vout"], text, fallback=_LABELED_NUM_FIRST["vout"])
    else:
        # labeled wins; else the "<V> to <V>" pair (in -> out)
        vin = _first_num(_LABELED["vin"], text, fallback=_LABELED_NUM_FIRST["vin"])
        vout = _first_num(_LABELED["vout"], text, fallback=_LABELED_NUM_FIRST["vout"])
        pair = _TO_PAIR.search(text)
        if pair:
            p_in, p_out = float(pair.group(1)), float(pair.group(2))
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

    # fsw: MHz / kHz / plain Hz (unit-specific patterns, first hit wins)
    fsw_khz: float | None = None
    fsw_m = _FSW_MHZ.search(text)
    if fsw_m:
        fsw_khz = float(fsw_m.group(1)) * 1e3
    else:
        fsw_m = _FSW_KHZ.search(text)
        if fsw_m:
            fsw_khz = float(fsw_m.group(1))
        else:
            fsw_m = _FSW_HZ.search(text)
            if fsw_m:
                fsw_khz = float(fsw_m.group(1)) / 1e3
    if fsw_khz is None:
        fsw_khz = 500.0  # documented default

    # ripple: mV wins over V (a "V" regex would also match the number in mV)
    rip_m = _RIPPLE_MV.search(text)
    rip_v = _RIPPLE_V.search(text)
    ripple_v = float(rip_m.group(1)) / 1000.0 if rip_m else (
        float(rip_v.group(1)) if rip_v else None)
    if ripple_v is None:
        ripple_v = 0.01 * vout  # provisional: 1% of Vout

    eff_pct = _first_num(_EFF, text, fallback=_EFF_NUM_FIRST)
    efficiency_target = eff_pct / 100.0 if eff_pct is not None else None

    tj = _first_num(_TJ, text, fallback=_TJ_WORDS)
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
    if vin_min is not None and vin_min >= vin_max:
        contradictions.append(
            f"input range {vin_min}-{vin_max} V is empty (min >= max)")
    if vin == vout and vin_min is None:
        contradictions.append("input and output voltage cannot be equal "
                              "(that is not a converter step)")
    vin_hi = vin_max if vin_max is not None else vin
    if uvlo is not None and uvlo > vin_hi:
        contradictions.append(
            f"UVLO {uvlo} V above the input maximum {vin_hi} V — "
            "the converter can never start")
    if ocp is not None and ocp <= iout:
        contradictions.append(
            f"OCP {ocp} A at/below nominal load {iout} A — protection would trip "
            "in normal operation")
    if contradictions:
        raise SpecParseError("; ".join(contradictions))

    req = Requirements(
        Vin=vin, Vout=vout, Iout=iout, fsw_khz=fsw_khz,
        ripple_v=ripple_v,
        efficiency_target=efficiency_target,
        tj_max_c=tj_max,
        vin_min=vin_min, vin_max=vin_max,
        ocp_a=ocp, otp_c=otp, uvlo_v=uvlo,
    )

    # --- question policy ---
    assumptions: list[str] = []
    missing_material: list[str] = []
    if in_range:
        assumptions.append(
            f"input range {vin_min}-{vin_max} V: designing at Vin={vin_max} V "
            "(worst-case voltage stress); the low corner is NOT sized yet — confirm")
        missing_material.append("input range corner policy")
    if rip_m is None and rip_v is None:
        assumptions.append(
            f"ripple not specified: assumed 1% of Vout = {ripple_v * 1e3:.1f} mV — confirm")
        missing_material.append("ripple")
    if tj is None:
        assumptions.append(
            "Tj limit not specified: assumed 150 degC (library MOSFET maximum — "
            "zero margin by construction) — confirm")
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