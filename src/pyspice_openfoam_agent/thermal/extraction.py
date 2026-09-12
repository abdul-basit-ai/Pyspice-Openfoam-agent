"""Phase 9: programmatic T-field extraction via PyVista's POpenFOAMReader.

Two extraction paths, per user decision (both kept):
  1. FAST PATH — solver-log Min/max T parsing (Phase 8's _extract_tj):
     zero cost, used by the agent loop's fast path and the mitigation loop.
  2. FULL FIELD — PyVista POpenFOAMReader over the case: per-region T on
     every cell, plus PNG snapshots for human review. This is the agent's
     deep-inspection path and the Phase 9 checkpoint validates that the two
     agree within tolerance.

API quirks discovered live in the container (pyvista 0.49 + v2406):
  - POpenFOAMReader requires the controlDict path (case/system/controlDict),
    NOT the case directory (the directory form hits a VTK API mismatch:
    SetDirectoryName missing in this VTK build).
  - reader.time_values lists the written time dirs; use
    set_active_time_value(latest) to read the converged state.
  - reader.read() returns a MultiBlock of per-region MultiBlocks; each
    region's T lives in region["internalMesh"].point_data["T"].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class ExtractionError(RuntimeError):
    """Field extraction failed (missing results, bad reader state)."""


@dataclass
class FieldSummary:
    """Per-region temperature summary of one converged CHT case."""

    time: float
    tj_per_device: dict[str, float]  # solid region -> max T (K)
    tj_max: float | None  # max over solids (K)
    region_stats: dict[str, dict[str, float]]  # region -> {min, max, mean}
    notes: list[str] = field(default_factory=list)


def _latest_time(case: Path) -> float:
    """Find the latest written time directory (numeric names only)."""
    times = sorted(
        float(d.name)
        for d in case.iterdir()
        if d.is_dir() and d.name.replace(".", "", 1).isdigit()
    )
    if not times:
        raise ExtractionError(f"no time directories in {case} — has the solver run?")
    return times[-1]


def extract_full_field(case: str | Path) -> FieldSummary:
    """Read the latest time step's T field for every region via PyVista.

    Returns per-region min/max/mean T plus per-solid-device Tj max. Raises
    ExtractionError when the case has no results.
    """
    import pyvista as pv

    case_path = Path(case)
    control_dict = case_path / "system" / "controlDict"
    if not control_dict.exists():
        raise ExtractionError(f"not an OpenFOAM case (no system/controlDict): {case_path}")

    reader = pv.POpenFOAMReader(str(control_dict))
    times = reader.time_values
    if not times:
        raise ExtractionError(f"reader found no time values in {case_path}")
    latest = times[-1]
    latest = times[-1]
    reader.set_active_time_value(latest)
    data = reader.read()

    tj_per_device: dict[str, float] = {}
    region_stats: dict[str, dict[str, float]] = {}

    for name in data.keys():
        block = data[name]
        if not hasattr(block, "keys"):
            continue
        internal = block["internalMesh"] if "internalMesh" in block.keys() else None
        if internal is None or "T" not in internal.point_data:
            continue
        arr = internal.point_data["T"]
        region_stats[name] = {
            "min": float(arr.min()),
            "max": float(arr.max()),
            "mean": float(arr.mean()),
            "n_cells": int(internal.n_cells),
        }
        if name != "air":
            tj_per_device[name] = float(arr.max())

    if not tj_per_device:
        raise ExtractionError(f"no T fields found in any solid region of {case_path}")

    return FieldSummary(
        time=float(latest),
        tj_per_device=tj_per_device,
        tj_max=max(tj_per_device.values()),
        region_stats=region_stats,
        notes=[f"extracted {len(region_stats)} regions via POpenFOAMReader at t={latest}"],
    )


def render_temperature_png(case: str | Path, out_png: str | Path) -> Path:
    """Optional human-review snapshot: the solid regions' T field, one image.

    The agent-facing deliverable is the scalar summary; this is for the
    Phase 11b HITL approval card.
    """
    import pyvista as pv

    case_path = Path(case)
    reader = pv.POpenFOAMReader(str(case_path / "system" / "controlDict"))
    if reader.time_values:
        reader.set_active_time_value(reader.time_values[-1])
    data = reader.read()

    plotter = pv.Plotter(off_screen=True)
    added = False
    for name in ("board", "hs_mosfet", "ls_mosfet", "inductor"):
        if name in data.keys() and "internalMesh" in data[name].keys():
            mesh = data[name]["internalMesh"]
            if "T" in mesh.point_data:
                plotter.add_mesh(mesh, scalars="T", cmap="inferno")
                added = True
    if not added:
        raise ExtractionError(f"no solid T fields to render in {case_path}")
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        plotter.screenshot(str(out))
    except Exception as e:  # headless container without OSMesa/EGL
        plotter.close()
        raise ExtractionError(
            f"PNG render failed (headless environment needs OSMesa or EGL): {e}"
        ) from e
    plotter.close()
    return out
