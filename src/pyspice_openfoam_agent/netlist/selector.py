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

from pyspice_openfoam_agent.library.loader import (
    Library,
    query_capacitors,
    query_controllers,
    query_gate_drivers,
    query_inductors,
    query_mosfets,
)
from pyspice_openfoam_agent.library.schema import (
    Capacitor,
    ControllerIC,
    GateDriver,
    Inductor,
    MOSFET,
)
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

    mosfet: MOSFET  # primary switch part (HS for buck/boost; leg-1 for buck_boost)
    inductor: Inductor
    capacitor: Capacitor
    # Per-switch support: an OPTIONAL second part for the other switch
    # position(s). Role mapping per topology:
    #   buck        mosfet = HS,            mosfet_second = LS
    #   boost       mosfet = ctrl (hard),   mosfet_second = sync
    #   buck_boost  mosfet = leg-1 (HS+LS_B), mosfet_second = leg-2 (LS_A+SYNC)
    # None (default) applies the primary part to every switch, preserving the
    # historical single-part behavior.
    mosfet_second: MOSFET | None = None
    # Phase 5 ICs (BOM lines only — the SPICE rig stays behavioral with ideal
    # switches, so the netlist never references these parts):
    gate_driver: GateDriver | None = None
    controller: ControllerIC | None = None
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


def _predicted_ripple_pp(spec: Spec, sizing: SizingResult, fsw: float,
                         c_eff: float, esr_eff: float) -> tuple[float, float, float]:
    """(total p-p, ESR term, capacitive term) predicted output ripple for an
    effective output capacitance/ESR — the same closed form the screening
    gate uses (buck: cap current swings di_pp; boost/buck_boost: the full
    inductor current I_L = Iout/(1-D) with Iout*D/(fsw*C) of storage swing)."""
    if sizing.topology == "buck":
        i_cap_pp = sizing.di_pp
        v_cap = sizing.di_pp / (8.0 * fsw * c_eff)
    else:
        d = min(max(sizing.D, 0.05), 0.95)
        i_cap_pp = spec.Iout / (1.0 - d)
        v_cap = spec.Iout * d / (fsw * c_eff)
    v_esr = i_cap_pp * esr_eff
    return v_esr + v_cap, v_esr, v_cap


def _banked_capacitor(base: Capacitor, n: int) -> Capacitor:
    """Synthesize an n-unit parallel bank of `base` as one Capacitor:
    C and Irms add, ESR and ESL divide. Paralleling identical ceramics is
    standard practice when no single part meets a tight ripple budget; the
    netlist emitter renders the bank as its exact single-branch equivalent
    (C_eff, ESR_eff in series — identical to n branches now that ESL is not
    emitted)."""
    return Capacitor(
        part_number=f"{n}x {base.part_number}",
        manufacturer=base.manufacturer,
        C=base.C * n,
        tol_percent=base.tol_percent,
        ESR=base.ESR / n,
        ESL=max(base.ESL / n, 0.0),
        V_rated=base.V_rated,
        Irms_max=base.Irms_max * n,
        package=base.package,
        datasheet=base.datasheet,
    )


def select_capacitor(lib: Library, spec: Spec, sizing: SizingResult,
                     cap_units_floor: int = 1) -> tuple[Capacitor, list[str]]:
    """Lowest-ESR selection (among parts that clear C/V_rated with margin),
    extended with MLCC BANKING when no single part can meet the spec's ripple
    budget (audit-run finding: the tightest budgets need ESR ~2-3 mOhm at
    10-12 A cap-current swing, and the best single bulk part is 10 mOhm).

    Selection order:
      1. single part meeting C_min*margin and V_rated*margin with the lowest
         ESR — kept as-is when its predicted ripple fits the budget;
      2. otherwise the lowest-ESR adequate-voltage part, paralleled N times
         (N solves C >= 1.2*C_min AND predicted ripple <= budget; capped at
         8 units) — returned as a synthesized bank Capacitor.
    """
    C_req = sizing.C_min * C_MARGIN
    vrated_req = spec.Vout * VRATED_MARGIN
    notes: list[str] = []

    def ripple_of(c: Capacitor) -> float:
        return _predicted_ripple_pp(spec, sizing, spec.fsw, c.C, c.ESR)[0]

    # Explicit upsize request (run_spice post-measurement retry): bank the
    # lowest-ESR adequate-voltage part with at least `cap_units_floor`
    # units, no budget test — the caller has MEASURED a violation and wants
    # more capacitance, not another prediction.
    if cap_units_floor > 1:
        pool = query_capacitors(lib, V_rated_min=vrated_req)
        if pool:
            base = min(pool, key=lambda c: c.ESR)
            cand = _banked_capacitor(base, cap_units_floor)
            notes.append(
                f"output capacitor bank upsized after measurement: "
                f"{cap_units_floor}x {base.part_number} "
                f"(C_eff {cand.C * 1e6:.0f} uF, ESR_eff {cand.ESR * 1e3:.2f} mOhm)"
            )
            return cand, notes

    hits = query_capacitors(lib, C_min=C_req, V_rated_min=vrated_req)
    best_single = min(hits, key=lambda c: c.ESR) if hits else None
    budget_ok = lambda c: (spec.Vripple <= 0) or (ripple_of(c) <= spec.Vripple)

    if best_single is not None and budget_ok(best_single):
        best = best_single
    else:
        # bank from the lowest-ESR adequate-voltage part (C_min filter dropped:
        # units add)
        pool = query_capacitors(lib, V_rated_min=vrated_req)
        if not pool:
            raise SelectionError(
                f"No capacitor in library with V_rated >= {vrated_req:.1f} V"
            )
        base = min(pool, key=lambda c: c.ESR)
        best = None
        for n in range(1, 9):
            cand = _banked_capacitor(base, n)
            if cand.C >= C_req and budget_ok(cand):
                best = cand
                notes.append(
                    f"output capacitor bank: {n}x {base.part_number} in parallel "
                    f"(C_eff {cand.C * 1e6:.0f} uF, ESR_eff {cand.ESR * 1e3:.2f} mOhm) — "
                    f"no single library part met the "
                    f"{spec.Vripple * 1e3:.0f} mV ripple budget"
                )
                break
        if best is None:
            # even 8 units cannot meet the budget: return the best-effort
            # 8-unit bank and let the screening gate reject the design with
            # the honest number (do NOT silently ship a bank that misses
            # the spec)
            best = best_single or _banked_capacitor(base, 8)
            notes.append(
                f"even an 8x {base.part_number} bank predicts "
                f"{ripple_of(best) * 1e3:.2f} mV pp ripple — the "
                f"{spec.Vripple * 1e3:.2f} mV budget is beyond this library; "
                f"screening will reject"
            )

    esr_ripple = _predicted_ripple_pp(spec, sizing, spec.fsw, best.C, best.ESR)[1]
    if esr_ripple > spec.Vripple:
        notes.append(
            f"ESR-driven ripple ({esr_ripple * 1e3:.1f} mV) with {best.part_number} alone exceeds "
            f"the Vripple budget ({spec.Vripple * 1e3:.1f} mV) -- consider a lower-ESR part or "
            f"paralleling capacitors"
        )
    return best, notes


# --- Phase 5 IC selection (gate driver + controller; BOM lines only) ---
# The SPICE rig models gates as behavioral 5 V / 2 ohm sources (builder.py's
# GATE_DRIVE_V / GATE_DRIVE_R). The IC selection mirrors that assumption: the
# driver must be able to deliver the rig's peak gate current at the chosen
# rail. Controllers are filtered to voltage-mode parts (the compensator this
# project designs is a Type III voltage-mode network).
_BUCK_ONLY_CONTROLLERS = {"TPS40057PWPR", "LM27402MHX"}  # buck controllers
_GENERIC_PWM_CONTROLLERS = {"SG3525A", "TL494"}  # usable in any VM topology


def select_gate_driver(lib: Library, mosfet: MOSFET, topology: str) -> tuple[GateDriver | None, list[str]]:
    """Pick a gate driver consistent with the rig's drive assumption.

    Buck: the HS switch floats on the switch node — a real design needs a
    half-bridge (bootstrap) driver. Boost/buck_boost: all switches are
    ground-referenced; two single low-side drivers (or one dual) suffice.
    Returns (driver_or_None, notes). None + note means the library has no
    fit — reported honestly, never silently skipped.
    """
    from pyspice_openfoam_agent.netlist.builder import GATE_DRIVE_R, GATE_DRIVE_V

    i_gate_req = max((GATE_DRIVE_V - mosfet.V_plateau) / GATE_DRIVE_R, 0.5)
    notes: list[str] = []
    if topology == "buck":
        half = query_gate_drivers(lib, half_bridge=True,
                                  peak_source_min=i_gate_req)
        if not half:
            # fall back to ANY half-bridge part even if peak current is shy
            half = query_gate_drivers(lib, half_bridge=True)
        if not half:
            notes.append("gate driver: no half-bridge driver in library — "
                         "synchronous buck needs one; IC omitted")
            return None, notes
        d = half[0]
        if not (d.drive_voltage_min_v <= GATE_DRIVE_V <= d.drive_voltage_max_v):
            notes.append(
                f"gate driver: {d.part_number} needs a {d.drive_voltage_min_v:.0f}-"
                f"{d.drive_voltage_max_v:.0f} V VDD rail (the SPICE rig's behavioral "
                f"5 V drive is a simulation convention; power it from an aux rail)"
            )
        else:
            notes.append(f"gate driver: {d.part_number} (half-bridge, "
                         f"{d.peak_source_a:.1f}A/{d.peak_sink_a:.1f}A, "
                         f"tpd {d.propagation_delay_ns:.0f} ns)")
        return d, notes
    # boost / buck_boost: ground-referenced switches -> low-side drivers,
    # rail window must contain the rig's 5 V, peak current >= rig demand.
    low = query_gate_drivers(lib, v_drive_min=GATE_DRIVE_V, v_drive_max=GATE_DRIVE_V,
                             peak_source_min=i_gate_req, half_bridge=False)
    if not low:
        notes.append(
            f"gate driver: no low-side driver in library covers a {GATE_DRIVE_V:.0f} V "
            f"rail at >= {i_gate_req:.1f} A peak — IC omitted"
        )
        return None, notes
    fastest = min(low, key=lambda g: g.propagation_delay_ns)
    notes.append(
        f"gate driver: {fastest.part_number} (low-side x2 for {topology}, "
        f"{fastest.peak_source_a:.1f}A/{fastest.peak_sink_a:.1f}A, "
        f"tpd {fastest.propagation_delay_ns:.0f} ns — fastest of "
        f"{len(low)} candidates covering the {GATE_DRIVE_V:.0f} V rail)"
    )
    return fastest, notes


def select_controller(lib: Library, spec: Spec, sizing: SizingResult) -> tuple[ControllerIC | None, list[str]]:
    """Pick a voltage-mode controller whose fsw window covers the design.

    Buck designs may use buck-specific controllers or generic PWM parts;
    boost/buck_boost get generic parts only (buck controllers' gate timing
    assumes a buck power stage). None + note = library gap, reported honestly.
    """
    notes: list[str] = []
    vm = query_controllers(lib, control_law="voltage", fsw_hz=spec.fsw)
    usable = [c for c in vm
              if sizing.topology == "buck" or c.part_number in _GENERIC_PWM_CONTROLLERS]
    if not usable:
        # best-effort: name the closest usable-class part and the gap instead
        # of silence (closest among GENERIC parts — buck controllers are not
        # candidates for boost/buck_boost however fast they are)
        generic = [c for c in query_controllers(lib, control_law="voltage")
                   if c.part_number in _GENERIC_PWM_CONTROLLERS]
        if generic:
            closest = max(generic, key=lambda c: c.fsw_max_hz)
            notes.append(
                f"controller: no voltage-mode library controller covers "
                f"{spec.fsw / 1e3:.0f} kHz for {sizing.topology} (closest generic: "
                f"{closest.part_number}, fsw <= {closest.fsw_max_hz / 1e3:.0f} kHz) — IC omitted"
            )
        else:
            notes.append("controller: library has no voltage-mode controllers — IC omitted")
        return None, notes
    # prefer the widest fsw headroom above the design point
    c = max(usable, key=lambda x: x.fsw_max_hz - spec.fsw)
    notes.append(
        f"controller: {c.part_number} (voltage-mode, fsw {c.fsw_min_hz / 1e3:.0f}-"
        f"{c.fsw_max_hz / 1e3:.0f} kHz vs design {spec.fsw / 1e3:.0f} kHz, "
        f"Vref {c.v_ref} V +/-{c.v_ref_tol_percent:.0f}%)"
    )
    return c, notes


def select_components(lib: Library, spec: Spec, sizing: SizingResult,
                      cap_units_floor: int = 1) -> SelectedComponents:
    """Run all selectors and collect their notes into one result."""
    mosfet = select_mosfet(lib, spec, sizing)
    inductor = select_inductor(lib, sizing)
    capacitor, cap_notes = select_capacitor(lib, spec, sizing,
                                            cap_units_floor=cap_units_floor)
    gate_driver, driver_notes = select_gate_driver(lib, mosfet, sizing.topology)
    controller, ctl_notes = select_controller(lib, spec, sizing)

    notes = list(cap_notes) + driver_notes + ctl_notes
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
    return SelectedComponents(mosfet=mosfet, inductor=inductor, capacitor=capacitor,
                              gate_driver=gate_driver, controller=controller, notes=notes)