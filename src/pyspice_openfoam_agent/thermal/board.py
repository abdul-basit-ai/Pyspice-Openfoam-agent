"""Phase 6: JEDEC JESD51-3 board geometry template.

A fixed, deterministic board definition (plan Phase 6): FR4 board at the
JESD51-3 standardized size with >=50 um top copper, plus a deterministic
component layout whose MOSFET zone boxes derive from the Phase 1 library's
die dimensions. This is a pure data structure -- no meshing here (Phase 7
turns it into blockMeshDict).

Why JESD51-3 dimensions (from the plan, with sources): the standard fixes
board size and copper weight *so that thermal results are comparable across
labs and against manufacturer R_theta_ja figures*. Using it makes the
simulated Tj directly comparable to the datasheet number for the same part.

Layout rationale: the synchronous buck has two distinct MOSFET heat sources
(HS, LS) even when both use the same part number -- Phase 5 already tags
their losses separately. Both sit in an airflow-aligned row (x = airflow
direction): HS first, then LS, with the inductor between/after them (the
inductor is a mild heat source: DCR loss only). Zones are centered on a
baseline line across the board's width, spaced with fixed gaps, all on the
top surface.

Fluid envelope (Phase 7's blockMesh domain): a wind-tunnel box around the
board -- inlet upstream, outlet downstream, generous clearance above and to
the sides. Fixed constants, tunable only by editing this module (they are
deliberately NOT spec-dependent: JEDEC comparability is the point).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyspice_openfoam_agent.library.schema import MOSFET


class LayoutError(ValueError):
    """Component layout does not fit the fixed JEDEC board."""


# --- JEDEC JESD51-3 constants (not tunable; comparability is the point) ---
BOARD_LENGTH_MM = 114.3  # x (airflow direction) — JESD51-3: 114.3 mm (4.5 in)
BOARD_WIDTH_MM = 76.2  # y — JESD51-3: 76.2 mm (3.0 in)
BOARD_THICKNESS_MM = 1.6  # FR4
TOP_COPPER_UM = 70.0  # >= 50 um per JESD51-3 (2 oz)
AMBIENT_TEMP_C = 25.0  # JEDEC still-air reference ambient

# --- fluid domain envelope (wind tunnel), fixed constants ---
INLET_UPSTREAM_MM = 20.0  # air enters this far before the board edge
OUTLET_DOWNSTREAM_MM = 20.0
DOMAIN_HEIGHT_MM = 30.0  # above the board
DOMAIN_SIDE_MARGIN_MM = 20.0  # each side beyond the board edge

# --- layout constants ---
LAYOUT_BASELINE_X_MM = 30.0  # first device's leading edge on the board
DEVICE_GAP_MM = 8.0  # gap between adjacent device zones
ZONE_MARGIN_MM = 2.0  # clearance pad around the bare die footprint


@dataclass(frozen=True)
class DeviceZone:
    """One heat-source footprint on the board (all mm, board coordinates:
    origin at board corner, x along airflow, y across, z up from board top)."""

    tag: str  # "hs_mosfet" | "ls_mosfet" | "inductor"
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_base_mm: float  # board top surface
    z_height_mm: float  # device height above the board

    @property
    def center(self) -> tuple[float, float, float]:
        return (
            (self.x_min + self.x_max) / 2,
            (self.y_min + self.y_max) / 2,
            self.z_base_mm + self.z_height_mm / 2,
        )

    @property
    def footprint_area_mm2(self) -> float:
        return (self.x_max - self.x_min) * (self.y_max - self.y_min)


@dataclass(frozen=True)
class BoardGeometry:
    """The complete Phase 6 output: fixed board + per-device zones + fluid
    envelope. Everything Phase 7's mesh generator needs, nothing more."""

    length_mm: float
    width_mm: float
    thickness_mm: float
    top_copper_um: float
    zones: dict[str, DeviceZone]
    domain: dict[str, float]  # wind-tunnel envelope, board coords

    def total_device_area_mm2(self) -> float:
        return sum(z.footprint_area_mm2 for z in self.zones.values())


def _place_row(
    devices: list[tuple[str, float, float, float]],  # (tag, dx_mm, dy_mm, height_mm)
    board_width_mm: float = BOARD_WIDTH_MM,
    board_length_mm: float = BOARD_LENGTH_MM,
) -> dict[str, DeviceZone]:
    """Place devices in a row along x at LAYOUT_BASELINE_X_MM, sharing the
    board's y centerline; verify the row fits the board."""
    zones: dict[str, DeviceZone] = {}
    x = LAYOUT_BASELINE_X_MM
    y_center = board_width_mm / 2.0
    for tag, dx, dy, height in devices:
        if x + dx > board_length_mm:
            raise LayoutError(
                f"device row exceeds board length: {tag} would end at {x + dx:.1f} mm "
                f"> {board_length_mm:.0f} mm"
            )
        if dy > board_width_mm:
            raise LayoutError(f"{tag} width {dy} mm exceeds board width {board_width_mm}")
        zones[tag] = DeviceZone(
            tag=tag,
            x_min=round(x, 4),
            x_max=round(x + dx, 4),
            y_min=round(y_center - dy / 2, 4),
            y_max=round(y_center + dy / 2, 4),
            z_base_mm=BOARD_THICKNESS_MM,  # sits on top of the 1.6mm board
            z_height_mm=height,
        )
        x += dx + DEVICE_GAP_MM
    return zones


def build_board_geometry(
    mosfet: MOSFET,
    inductor_x_mm: float = 12.0,
    inductor_y_mm: float = 12.0,
    inductor_height_mm: float = 4.0,
) -> BoardGeometry:
    """Deterministic JEDEC layout for the synchronous buck's heat sources.

    Both switches share the selected MOSFET's die footprint (Phase 5 tags
    them as separate heat sources even when the part number matches). The
    inductor's footprint comes from its package size -- the caller passes
    nominal catalog dims (defaults: 12x12 mm for the 1010-class parts the
    selector picks at these currents); they are parameters, not library
    fields, because Phase 1 deliberately didn't carry inductor footprints.
    """
    zones = _place_row(
        [
            ("hs_mosfet", mosfet.die_x_mm, mosfet.die_y_mm, mosfet.die_z_mm),
            ("ls_mosfet", mosfet.die_x_mm, mosfet.die_y_mm, mosfet.die_z_mm),
            ("inductor", inductor_x_mm, inductor_y_mm, inductor_height_mm),
        ],
        BOARD_WIDTH_MM,
    )

    domain = {
        "x_min_mm": -INLET_UPSTREAM_MM,
        "x_max_mm": BOARD_LENGTH_MM + OUTLET_DOWNSTREAM_MM,
        "y_min_mm": -DOMAIN_SIDE_MARGIN_MM,
        "y_max_mm": BOARD_WIDTH_MM + DOMAIN_SIDE_MARGIN_MM,
        "z_min_mm": 0.0,
        "z_max_mm": BOARD_THICKNESS_MM + DOMAIN_HEIGHT_MM,
        "inlet_normal": "+x",
        "outlet_normal": "-x",
    }
    return BoardGeometry(
        length_mm=BOARD_LENGTH_MM,
        width_mm=BOARD_WIDTH_MM,
        thickness_mm=BOARD_THICKNESS_MM,
        top_copper_um=TOP_COPPER_UM,
        zones=zones,
        domain=domain,
    )