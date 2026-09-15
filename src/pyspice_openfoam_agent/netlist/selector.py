"""Phase 3: Map analytically-sized L/C to the closest real part, with margin.

Why margin, not nearest value (either direction) -- see phase plan Phase 3:
real inductors/capacitors carry tolerance bands (commonly +/-20%), so picking
a part right at the analytical minimum risks silent under-sizing once
tolerance stack-up is accounted for. Every selector below asks the Phase 1
library for a part that clears the requirement by a fixed margin, then keeps
the cheapest/smallest part that clears it (query_* functions already sort
ascending), except the MOSFET (sorted by Rds_on, so the first hit is also the
lowest-conduction-loss part that meets the voltage/current floor) and the
capacitor (re-ranked by ESR, since ESR is the dominant ripple/loss term the
sizing engine deliberately left for this phase to resolve with a real part).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyspice_openfoam_agent.library.loader import Library, query_capacitors, query_inductors, query_mosfets
from pyspice_openfoam_agent.library.schema import Capacitor, Inductor, MOSFET
from pyspice_openfoam_agent.sizing.engine import Spec, SizingResult

# Margin factors. Each is a documented, conservative choice -- not tuned to
# make any particular library part win.
L_MARGIN = 1.20  # L >= 1.20 * L_min: covers a -20% inductance tolerance part
C_MARGIN = 1.20  # C >= 1.20 * C_min: same logic for capacitance tolerance
ISAT_MARGIN = 1.20  # inductor Isat >= 1.20 * I_peak
IRMS_MARGIN = 1.10  # inductor Irms >= 1.10 * I_L_avg
VDS_MARGIN = 1.20  # MOSFET Vds_max >= 1.20 * blocking voltage (ringing headroom)
ID_MARGIN = 1.30  # MOSFET Id_max >= 1.30 * I_peak
VRATED_MARGIN = 1.20  # capacitor V_rated >= 1.20 * Vout


class SelectionError(ValueError):
    """No part in the Phase 1 library satisfies the derived requirement, even with margin."""


@dataclass
class SelectedComponents:
    """Real parts chosen for one sizing result, plus any selection-time warnings."""

    mosfet: MOSFET  # same part number used for both switches (synchronous topology)
    inductor: Inductor
    capacitor: Capacitor
    notes: list[str] = field(default_factory=list)


def _blocking_voltage(spec: Spec, sizing: SizingResult) -> float:
    """Worst-case steady-state voltage a switch in this topology must block.

    Buck: switches block Vin. Boost: the switch node swings to Vout, the
    higher rail. Buck-boost: Phase 3 emits the 4-switch NON-INVERTING
    topology, where the left pair (HS/LS_A) blocks ~Vin and the right pair
    (SYNC/LS_B) blocks ~Vout -- never the sum (that was the classic
    inverting-topology figure; stale since the builder was corrected).
    """
    if sizing.topology == "boost":
        return spec.Vout
    if sizing.topology == "buck_boost":
        return max(spec.Vin, spec.Vout)
    return spec.Vin


def select_mosfet(lib: Library, spec: Spec, sizing: SizingResult) -> MOSFET:
    """Lowest-Rds_on part that clears the voltage/current floor with margin."""
    vds_req = _blocking_voltage(spec, sizing) * VDS_MARGIN
    id_req = sizing.I_peak * ID_MARGIN
    hits = query_mosfets(lib, Vds_min=vds_req, Id_min=id_req)
    if not hits:
        raise SelectionError(
            f"No MOSFET in library with Vds_max >= {vds_req:.1f} V and Id_max >= {id_req:.1f} A "
            f"(topology={sizing.topology}, I_peak={sizing.I_peak:.2f} A)"
        )
    return hits[0]  # query_mosfets sorts ascending by Rds_on


def select_inductor(lib: Library, sizing: SizingResult) -> Inductor:
    """Smallest-L part (by value, i.e. cheapest/smallest footprint) that clears
    the L/Isat/Irms floor with margin.
    """
    L_req = sizing.L_min * L_MARGIN
    isat_req = sizing.I_peak * ISAT_MARGIN
    irms_req = sizing.I_L_avg * IRMS_MARGIN
    hits = query_inductors(lib, L_min=L_req, Isat_min=isat_req, Irms_min=irms_req)
    if not hits:
        raise SelectionError(
            f"No inductor in library with L >= {L_req * 1e6:.2f} uH, "
            f"Isat >= {isat_req:.1f} A, Irms >= {irms_req:.1f} A"
        )
    return hits[0]  # query_inductors sorts ascending by L


def select_capacitor(lib: Library, spec: Spec, sizing: SizingResult) -> tuple[Capacitor, list[str]]:
    """Lowest-ESR part (among those that clear C/V_rated with margin).

    ESR is deliberately not enforced by the sizing engine (see engine.py's
    module docstring) -- it needs a real part. Once one is picked, check the
    ESR-driven ripple contribution against the spec's ripple budget and warn
    (not fail) if it alone would blow the budget, since C_min + ESR together
    determine actual ripple and only the C-term was sized analytically.
    """
    C_req = sizing.C_min * C_MARGIN
    vrated_req = spec.Vout * VRATED_MARGIN
    hits = query_capacitors(lib, C_min=C_req, V_rated_min=vrated_req)
    if not hits:
        raise SelectionError(
            f"No capacitor in library with C >= {C_req * 1e6:.1f} uF and V_rated >= {vrated_req:.1f} V"
        )
    best = min(hits, key=lambda c: c.ESR)
    notes: list[str] = []
    esr_ripple = sizing.di_pp * best.ESR
    if esr_ripple > spec.Vripple:
        notes.append(
            f"ESR-driven ripple ({esr_ripple * 1e3:.1f} mV) with {best.part_number} alone exceeds "
            f"the Vripple budget ({spec.Vripple * 1e3:.1f} mV) -- consider a lower-ESR part or "
            f"paralleling capacitors"
        )
    return best, notes


def select_components(lib: Library, spec: Spec, sizing: SizingResult) -> SelectedComponents:
    """Run all three selectors and collect their notes into one result."""
    mosfet = select_mosfet(lib, spec, sizing)
    inductor = select_inductor(lib, sizing)
    capacitor, cap_notes = select_capacitor(lib, spec, sizing)

    notes = list(cap_notes)
    notes.append(
        f"MOSFET: {mosfet.part_number} (Rds_on={mosfet.Rds_on * 1e3:.2f} mOhm, "
        f"Vds_max={mosfet.Vds_max:.0f} V, Id_max={mosfet.Id_max:.0f} A)"
    )
    notes.append(
        f"Inductor: {inductor.part_number} (L={inductor.L * 1e6:.2f} uH vs L_min="
        f"{sizing.L_min * 1e6:.2f} uH, margin {inductor.L / sizing.L_min:.2f}x)"
    )
    notes.append(
        f"Capacitor: {capacitor.part_number} (C={capacitor.C * 1e6:.1f} uF vs C_min="
        f"{sizing.C_min * 1e6:.1f} uF, margin {capacitor.C / sizing.C_min:.2f}x, "
        f"ESR={capacitor.ESR * 1e3:.1f} mOhm)"
    )
    return SelectedComponents(mosfet=mosfet, inductor=inductor, capacitor=capacitor, notes=notes)