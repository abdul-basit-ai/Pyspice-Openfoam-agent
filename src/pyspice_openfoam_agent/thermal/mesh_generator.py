"""Phase 7: parametric CHT mesh planning + blockMeshDict generation.

Design decisions (user-locked, see plan Phase 7):
- ONE structured multi-block mesh over the whole domain; per-block `zone`
  keyword tags cellZones; `splitMeshRegions -cellZones` splits into regions.
  Verified live in v2406: blockMesh writes per-block zones into cellZones.
- Deviation from plan noted: blockMeshDict text is generated directly from
  the MeshPlan data object rather than through a Jinja2 template — every
  vertex is a computed value, so a template adds a failure mode and nothing
  else. (Jinja2 remains in the plan for future truly-template files.)
- Resolution (user decision: budget wins): 0.5 mm on device footprints and
  4 mm near field, 1 mm medium, 2 mm far field; z graded similarly.
- Conformality: a single global x/y/z partition; every block face matches
  its neighbor exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyspice_openfoam_agent.thermal.board import BoardGeometry

AIR_ZONE = "air"
BOARD_ZONE = "board"

FINE_MM = 0.5      # device footprints + near field
MEDIUM_MM = 1.0    # 4-8 mm from any device
COARSE_MM = 2.0    # farther than 8 mm
NEAR_MARGIN_MM = 4.0
MID_MARGIN_MM = 8.0


class MeshPlanError(ValueError):
    """Geometry cannot be partitioned into a valid structured mesh."""


@dataclass(frozen=True)
class BlockDef:
    ix: tuple[int, int]  # x-partition index range [i0, i1)
    iy: tuple[int, int]
    iz: tuple[int, int]
    zone: str


@dataclass
class MeshPlan:
    x_edges_mm: list[float]
    y_edges_mm: list[float]
    z_edges_mm: list[float]
    cells_x: list[int]
    cells_y: list[int]
    cells_z: list[int]
    blocks: list[BlockDef]

    @property
    def n_cells(self) -> int:
        return sum(
            self.cells_x[b.ix[0]] * self.cells_y[b.iy[0]] * self.cells_z[b.iz[0]]
            for b in self.blocks
        )

    @property
    def n_blocks(self) -> int:
        return len(self.blocks)


def _cells_for(width_mm: float, proximity_mm: float) -> int:
    """Cell count for one segment given its width and distance to the
    nearest device footprint."""
    if proximity_mm <= NEAR_MARGIN_MM:
        target = FINE_MM
    elif proximity_mm <= MID_MARGIN_MM:
        target = MEDIUM_MM
    else:
        target = COARSE_MM
    return max(2, round(width_mm / target))



def plan_mesh(geo: BoardGeometry) -> MeshPlan:
    """Compute the global partitions and per-block zone assignment.

    Z-structure: [0, board_top] = board; then bands split at every distinct
    device top; a device occupies its footprint from the board top to ITS
    top only; everything else in every band is air.
    """
    # collect partition edges
    xs = {0.0, geo.length_mm}
    ys = {0.0, geo.width_mm}
    device_tops: set[float] = set()
    for z in geo.zones.values():
        xs.add(round(z.x_min, 6))
        xs.add(round(z.x_max, 6))
        ys.add(round(z.y_min, 6))
        ys.add(round(z.y_max, 6))
        device_tops.add(round(z.z_base_mm + z.z_height_mm, 6))
    xs_edges = sorted(xs)
    ys_edges = sorted(ys)

    # z partition: board layer + bands between distinct device tops
    z_edges = [0.0, geo.thickness_mm]
    lo = geo.thickness_mm
    for top in sorted(device_tops):
        if top > lo + 1e-9:
            z_edges.append(top)
            lo = top
    tunnel_top = geo.domain["z_max_mm"]
    if z_edges[-1] < tunnel_top - 1e-9:
        z_edges.append(tunnel_top)

    # per-segment cell counts (x/y shared across all z layers: conformality)
    cells_x = [
        _cells_for(x1 - x0, _segment_proximity_x(x0, x1, geo))
        for x0, x1 in zip(xs_edges, xs_edges[1:])
    ]
    cells_y = [
        _cells_for(y1 - y0, _segment_proximity_y(y0, y1, geo))
        for y0, y1 in zip(ys_edges, ys_edges[1:])
    ]
    cells_z = [
        max(3, round((z1 - z0) / 0.4)) if k == 0
        else max(2, round((z1 - z0) / 0.5)) if (z1 - z0) <= 4.1
        else max(8, round((z1 - z0) / 1.3))
        for k, (z0, z1) in enumerate(zip(z_edges, z_edges[1:]))
    ]

    # zone assignment per (i, j, k) block
    blocks: list[BlockDef] = []
    nx, ny, nz = len(cells_x), len(cells_y), len(cells_z)
    for k in range(nz):
        z_mid = (z_edges[k] + z_edges[k + 1]) / 2
        for i in range(nx):
            x_mid = (xs_edges[i] + xs_edges[i + 1]) / 2
            for j in range(ny):
                y_mid = (ys_edges[j] + ys_edges[j + 1]) / 2
                if k == 0:
                    zone = BOARD_ZONE
                else:
                    zone = AIR_ZONE
                    for tag, z in geo.zones.items():
                        if (
                            z.x_min <= x_mid_ok(xs_edges, i) <= z.x_max
                            and z.y_min <= y_mid_ok(ys_edges, j) <= z.y_max
                            and z.z_base_mm - 1e-9 <= z_mid <= z.z_base_mm + z.z_height_mm + 1e-9
                        ):
                            zone = tag
                            break
                blocks.append(BlockDef((i, i + 1), (j, j + 1), (k, k + 1), zone))

    plan = MeshPlan(
        x_edges_mm=xs_edges,
        y_edges_mm=ys_edges,
        z_edges_mm=z_edges,
        cells_x=cells_x,
        cells_y=cells_y,
        cells_z=cells_z,
        blocks=blocks,
    )
    if plan.n_cells == 0:
        raise MeshPlanError("empty mesh plan")
    return plan


def x_mid_ok(edges: list[float], i: int) -> float:
    return (edges[i] + edges[i + 1]) / 2


def y_mid_ok(edges: list[float], j: int) -> float:
    return (edges[j] + edges[j + 1]) / 2


def _segment_proximity_x(x0: float, x1: float, geo: BoardGeometry) -> float:
    """Distance from an x-segment's CENTER to the nearest device footprint.
    Center-based (not edge-based): a wide far-field segment must not inherit
    FINE resolution just because it touches a device edge; resolution is a
    per-segment property, and the segment IS the refinement unit (audit
    finding: edge-based proximity gave a 1.1M-cell mesh, 2x over budget).
    Segments covering a device footprint have center inside it -> distance 0
    -> FINE. Devices span the y-centerline, so x-proximity is a good proxy;
    the y ladder uses the analogous function."""
    best = float("inf")
    center = (x0 + x1) / 2
    for z in geo.zones.values():
        dx = max(z.x_min - center, 0.0, center - z.x_max)
        best = min(best, dx)
    return best


def _segment_proximity_y(y0: float, y1: float, geo: BoardGeometry) -> float:
    best = float("inf")
    center = (y0 + y1) / 2
    for z in geo.zones.values():
        dy = max(z.y_min - center, 0.0, center - z.y_max)
        best = min(best, dy)
    return best

# ---------------- blockMeshDict rendering ----------------


def _vertex_index(plan: "MeshPlan", i: int, j: int, k: int) -> int:
    """Global vertex id: x-major, then y, then z (matching the vertex loop)."""
    return (i * len(plan.y_edges_mm) + j) * len(plan.z_edges_mm) + k


def render_block_mesh_dict(plan: "MeshPlan") -> str:
    """Render MeshPlan as blockMeshDict text (mm in, convertToMeters 0.001).

    Boundary patches on the GLOBAL mesh (splitMeshRegions reassigns faces
    per region afterwards):
      inlet (x=0), outlet (x=length), topWall (z=tunnel top),
      sideWall_y0 / sideWall_y1 (y=0 / y=width), bottomWall (z=0).
    Internal faces between blocks of different zones become the inter-region
    coupled interfaces after splitMeshRegions -cellZones.
    """
    nx, ny, nz = len(plan.cells_x), len(plan.cells_y), len(plan.cells_z)

    lines: list[str] = []
    ap = lines.append
    ap("FoamFile")
    ap("{")
    ap("    version     2.0;")
    ap("    format      ascii;")
    ap("    class       dictionary;")
    ap("    object      blockMeshDict;")
    ap("}")
    ap("convertToMeters 0.001;")
    ap("")
    ap("vertices")
    ap("(")
    for i in range(nx + 1):
        for j in range(ny + 1):
            for k in range(nz + 1):
                ap(
                    f"    ({plan.x_edges_mm[i]:.6f} "
                    f"{plan.y_edges_mm[j]:.6f} {plan.z_edges_mm[k]:.6f})"
                )
    ap(");")
    ap("")
    ap("blocks")
    ap("(")
    for b in plan.blocks:
        v = [
            _vertex_index(plan, b.ix[0], b.iy[0], b.iz[0]),
            _vertex_index(plan, b.ix[1], b.iy[0], b.iz[0]),
            _vertex_index(plan, b.ix[1], b.iy[1], b.iz[0]),
            _vertex_index(plan, b.ix[0], b.iy[1], b.iz[0]),
            _vertex_index(plan, b.ix[0], b.iy[0], b.iz[1]),
            _vertex_index(plan, b.ix[1], b.iy[0], b.iz[1]),
            _vertex_index(plan, b.ix[1], b.iy[1], b.iz[1]),
            _vertex_index(plan, b.ix[0], b.iy[1], b.iz[1]),
        ]
        cx = plan.cells_x[b.ix[0]]
        cy = plan.cells_y[b.iy[0]]
        cz = plan.cells_z[b.iz[0]]
        ap(
            f"    hex ({v[0]} {v[1]} {v[2]} {v[3]} {v[4]} {v[5]} {v[6]} {v[7]}) "
            f"{b.zone} ({cx} {cy} {cz}) simpleGrading (1 1 1)"
        )
    ap(");")
    ap("")

    # ---- boundary patches (global-mesh outer faces) ----
    def face(i, j, k, which):  # returns 4 vertex ids of one boundary face
        if which == "x_min":
            return [ _vertex_index(plan, i, j, k), _vertex_index(plan, i, j + 1, k),
                     _vertex_index(plan, i, j + 1, k + 1), _vertex_index(plan, i, j, k + 1) ]
        if which == "x_max":
            return [ _vertex_index(plan, i, j, k), _vertex_index(plan, i, j + 1, k),
                     _vertex_index(plan, i, j + 1, k + 1), _vertex_index(plan, i, j, k + 1) ]
        raise ValueError(which)

    # plane faces as block-face index tuples (blockMesh boundary uses
    # vertex quadruples, ordering matters): build per-plane directly.
    ap("boundary")
    ap("(")
    # inlet: all faces on x = x_edges[0]
    ap("    inlet")
    ap("    {")
    ap("        type patch;")
    ap("        faces")
    ap("        (")
    for j in range(ny):
        for k in range(nz):
            a = _vertex_index(plan, 0, j, k)
            b_ = _vertex_index(plan, 0, j + 1, k)
            c = _vertex_index(plan, 0, j + 1, k + 1)
            d = _vertex_index(plan, 0, j, k + 1)
            ap(f"        ({a} {b_} {c} {d})")
    ap("        );")
    ap("    }")
    # outlet
    ap("    outlet")
    ap("    {")
    ap("        type patch;")
    ap("        faces")
    ap("        (")
    for j in range(ny):
        for k in range(nz):
            a = _vertex_index(plan, nx, j, k)
            b_ = _vertex_index(plan, nx, j + 1, k)
            c = _vertex_index(plan, nx, j + 1, k + 1)
            d = _vertex_index(plan, nx, j, k + 1)
            ap(f"        ({a} {b_} {c} {d})")
    ap("        );")
    ap("    }")
    # top wall (z max)
    ap("    topWall")
    ap("    {")
    ap("        type wall;")
    ap("        faces")
    ap("        (")
    for i in range(nx):
        for j in range(ny):
            a = _vertex_index(plan, i, j, nz)
            b_ = _vertex_index(plan, i + 1, j, nz)
            c = _vertex_index(plan, i + 1, j + 1, nz)
            d = _vertex_index(plan, i, j + 1, nz)
            ap(f"        ({a} {b_} {c} {d})")
    ap("        );")
    ap("    }")
    # side walls y0 / y1
    for name, jy in (("sideWall_y0", 0), ("sideWall_y1", ny)):
        ap(f"    {name}")
        ap("    {")
        ap("        type wall;")
        ap("        faces")
        ap("        (")
        for i in range(nx):
            for k in range(nz):
                a = _vertex_index(plan, i, jy, k)
                b_ = _vertex_index(plan, i + 1, jy, k)
                c = _vertex_index(plan, i + 1, jy, k + 1)
                d = _vertex_index(plan, i, jy, k + 1)
                ap(f"        ({a} {b_} {c} {d})")
        ap("        );")
        ap("    }")
    # bottom wall (z = 0)
    ap("    bottomWall")
    ap("    {")
    ap("        type wall;")
    ap("        faces")
    ap("        (")
    for i in range(nx):
        for j in range(ny):
            a = _vertex_index(plan, i, j, 0)
            b_ = _vertex_index(plan, i + 1, j, 0)
            c = _vertex_index(plan, i + 1, j + 1, 0)
            d = _vertex_index(plan, i, j + 1, 0)
            ap(f"        ({a} {b_} {c} {d})")
    ap("        );")
    ap("    }")
    ap(");")
    ap("")
    ap("mergePatchPairs ();")
    return "\n".join(lines) + "\n"
