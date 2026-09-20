"""Phase 6 standalone checkpoint tests.

The plan's checkpoint: generate the geometry data structure for 2-3
different MOSFET packages from the library and confirm placement coordinates
are computed correctly (no overlap, correct centering) -- purely a data/math
check, no simulation.

Hand-checked anchors:
- JESD51-3 board is 76 x 114 mm; JEDEC copper >= 50 um.
- CSD16415Q5 package footprint 5.0 x 6.0 mm -> HS zone [30.0, 35.0] x [35.1, 41.1].
- Row order HS, LS, inductor (12x12 default) with 8 mm gaps:
  HS ends 35.0, LS spans [43.0, 48.0], inductor spans [56.0, 68.0].
  All inside 114 mm length. Center y = 38.1 for all.
"""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.thermal.board import (
    BOARD_LENGTH_MM,
    BOARD_THICKNESS_MM,
    BOARD_WIDTH_MM,
    DEVICE_GAP_MM,
    LAYOUT_BASELINE_X_MM,
    TOP_COPPER_UM,
    BoardGeometry,
    DeviceZone,
    LayoutError,
    build_board_geometry,
)


@pytest.fixture(scope="module")
def lib():
    return load_library()


def _zones_in_board_order(geo: BoardGeometry) -> list[DeviceZone]:
    return sorted(geo.zones.values(), key=lambda z: z.x_min)


def test_jedec_constants_are_the_standard() -> None:
    # JESD51-3 low-K board is 4.5" x 3" = 114.3 x 76.2 mm
    assert (BOARD_LENGTH_MM, BOARD_WIDTH_MM) == (114.3, 76.2)
    assert BOARD_THICKNESS_MM == 1.6
    assert TOP_COPPER_UM >= 50.0


def test_three_packages_place_without_overlap(lib) -> None:
    """The plan's checkpoint: 2-3 different MOSFET packages, no overlap,
    correct centering."""
    parts = [
        lib.mosfets["CSD16415Q5"],  # TI SON5x6, footprint 5.0 x 6.0
        lib.mosfets["CSD16321Q5"],  # TI SON5x6, footprint 5.0 x 6.0
        lib.mosfets["CSD19531KCS"],  # TI TO-220, footprint 10.0 x 8.7
    ]
    for m in parts:
        geo = build_board_geometry(m)
        zs = _zones_in_board_order(geo)
        # exactly the three heat sources, on the board top
        assert [z.tag for z in zs] == ["hs_mosfet", "ls_mosfet", "inductor"]
        # no overlap: each zone starts at least one gap after the previous ends
        for a, b in zip(zs, zs[1:]):
            assert b.x_min >= a.x_max + DEVICE_GAP_MM - 1e-9
        # all zones inside the board, sitting on the top surface
        for z in geo.zones.values():
            assert 0 <= z.x_min < z.x_max <= BOARD_LENGTH_MM
            assert 0 <= z.y_min < z.y_max <= BOARD_WIDTH_MM
            assert z.z_base_mm == BOARD_THICKNESS_MM
            assert z.z_height_mm > 0


def test_mosfet_zone_uses_die_dimensions() -> None:
    m = load_library().mosfets["CSD16415Q5"]
    geo = build_board_geometry(m)
    hs = geo.zones["hs_mosfet"]
    assert hs.x_max - hs.x_min == pytest.approx(m.die_x_mm)
    assert hs.y_max - hs.y_min == pytest.approx(m.die_y_mm)
    assert hs.z_height_mm == m.die_z_mm
    # y-centered on the board
    assert hs.y_min == (BOARD_WIDTH_MM - m.die_y_mm) / 2
    # first device at the baseline
    assert hs.x_min == LAYOUT_BASELINE_X_MM


def test_row_positions_are_deterministic() -> None:
    m = load_library().mosfets["CSD16415Q5"]
    geo = build_board_geometry(m)
    hs, ls, ind = _zones_in_board_order(geo)
    assert ls.x_min == hs.x_max + DEVICE_GAP_MM
    assert ind.x_min == ls.x_max + DEVICE_GAP_MM
    # all share the same y centerline
    for z in (hs, ls, ind):
        assert z.y_min == BOARD_WIDTH_MM / 2 - (z.y_max - z.y_min) / 2


def test_row_overflow_raises() -> None:
    m = load_library().mosfets["CSD16415Q5"]
    # 3 giant inductors in a row blow past the 114mm board
    with pytest.raises(LayoutError, match="exceeds board length"):
        build_board_geometry(m, inductor_x_mm=80.0)


def test_fluid_envelope_covers_board_with_margins() -> None:
    m = load_library().mosfets["CSD16415Q5"]
    geo = build_board_geometry(m)
    dom = geo.domain
    assert dom["x_min_mm"] < 0 < BOARD_LENGTH_MM < dom["x_max_mm"]
    assert dom["y_min_mm"] < 0 < BOARD_WIDTH_MM < dom["y_max_mm"]
    assert dom["z_max_mm"] > BOARD_THICKNESS_MM
    # inlet/outlet convention: flow enters at low x, exits at high x
    assert dom["x_min_mm"] < dom["x_max_mm"]
