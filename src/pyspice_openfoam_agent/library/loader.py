"""Phase 1 library loader and query API.

Usage:
    from pyspice_openfoam_agent.library.loader import load_library, query_mosfets

    lib = load_library()  # all categories
    hits = query_mosfets(lib, Vds_min=40, Rds_on_max=5e-3)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pyspice_openfoam_agent.library.schema import (
    Capacitor,
    ControllerIC,
    Diode,
    GateDriver,
    Inductor,
    MOSFET,
)

LIBRARY_DIR = Path(__file__).parent / "data"

_FILES = {
    "mosfets": (MOSFET, "mosfets.yaml"),
    "inductors": (Inductor, "inductors.yaml"),
    "capacitors": (Capacitor, "capacitors.yaml"),
    "gate_drivers": (GateDriver, "gate_drivers.yaml"),
    "controllers": (ControllerIC, "controllers.yaml"),
    "diodes": (Diode, "diodes.yaml"),
}


class Library:
    """In-memory view of the curated library."""

    def __init__(self) -> None:
        self.mosfets: dict[str, MOSFET] = {}
        self.inductors: dict[str, Inductor] = {}
        self.capacitors: dict[str, Capacitor] = {}
        self.gate_drivers: dict[str, GateDriver] = {}
        self.controllers: dict[str, ControllerIC] = {}
        self.diodes: dict[str, Diode] = {}

    def counts(self) -> dict[str, int]:
        return {
            "mosfets": len(self.mosfets),
            "inductors": len(self.inductors),
            "capacitors": len(self.capacitors),
            "gate_drivers": len(self.gate_drivers),
            "controllers": len(self.controllers),
            "diodes": len(self.diodes),
        }


def load_library(library_dir: Path | None = None) -> Library:
    """Load all YAML files from the data dir into validated model objects."""
    d = Path(library_dir) if library_dir else LIBRARY_DIR
    lib = Library()
    for category, (model, filename) in _FILES.items():
        path = d / filename
        if not path.exists():
            if category in ("gate_drivers", "controllers", "diodes"):
                continue  # optional categories: populate as data is curated
            raise FileNotFoundError(f"Library file missing: {path}")
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        parts_raw = raw.get("parts", [])
        if not parts_raw:
            raise ValueError(f"Library file {path} has no parts")
        target = getattr(lib, category)
        seen: set[str] = set()
        for entry in parts_raw:
            part = model.model_validate(entry)
            if part.part_number in seen:
                raise ValueError(f"Duplicate part_number {part.part_number} in {path}")
            seen.add(part.part_number)
            target[part.part_number] = part
    return lib


def query_mosfets(
    lib: Library,
    Vds_min: float | None = None,
    Vds_max: float | None = None,
    Rds_on_max: float | None = None,
    Id_min: float | None = None,
    package: str | None = None,
) -> list[MOSFET]:
    """Filter MOSFETs by rating. Returns sorted by Rds_on (ascending)."""
    hits = []
    for m in lib.mosfets.values():
        if Vds_min is not None and m.Vds_max < Vds_min:
            continue
        if Vds_max is not None and m.Vds_max > Vds_max:
            continue
        if Rds_on_max is not None and m.Rds_on > Rds_on_max:
            continue
        if Id_min is not None and m.Id_max < Id_min:
            continue
        if package is not None and m.package != package:
            continue
        hits.append(m)
    return sorted(hits, key=lambda m: m.Rds_on)


def query_inductors(
    lib: Library,
    L_min: float | None = None,
    L_max: float | None = None,
    Isat_min: float | None = None,
    Irms_min: float | None = None,
) -> list[Inductor]:
    """Filter inductors. Returns sorted by L (ascending)."""
    hits = []
    for ind in lib.inductors.values():
        if L_min is not None and ind.L < L_min:
            continue
        if L_max is not None and ind.L > L_max:
            continue
        if Isat_min is not None and ind.Isat < Isat_min:
            continue
        if Irms_min is not None and ind.Irms < Irms_min:
            continue
        hits.append(ind)
    return sorted(hits, key=lambda x: x.L)


def query_capacitors(
    lib: Library,
    C_min: float | None = None,
    C_max: float | None = None,
    V_rated_min: float | None = None,
    ESR_max: float | None = None,
) -> list[Capacitor]:
    """Filter capacitors. Returns sorted by C (ascending)."""
    hits = []
    for c in lib.capacitors.values():
        if C_min is not None and c.C < C_min:
            continue
        if C_max is not None and c.C > C_max:
            continue
        if V_rated_min is not None and c.V_rated < V_rated_min:
            continue
        if ESR_max is not None and c.ESR > ESR_max:
            continue
        hits.append(c)
    return sorted(hits, key=lambda x: x.C)


def query_gate_drivers(
    lib: Library,
    v_drive_min: float | None = None,
    v_drive_max: float | None = None,
    peak_source_min: float | None = None,
    half_bridge: bool | None = None,
) -> list[GateDriver]:
    """Filter gate drivers by drive-voltage window and peak current.

    `half_bridge=True` selects only parts that drive a high-side + low-side
    pair from one IC (the synchronous-buck configuration); False selects
    low-side single-channel parts. None = no topology filter (the library
    encodes the distinction via part notes; the LM5107 is the half-bridge).
    Returns sorted by peak source current (descending) — stronger drive
    first, which is the ordering the Phase 5 selection wants.
    """
    _HALF_BRIDGE = {"LM5107MAX"}
    hits = []
    for g in lib.gate_drivers.values():
        if v_drive_min is not None and g.drive_voltage_min_v > v_drive_min:
            continue  # rail too low for this part
        if v_drive_max is not None and g.drive_voltage_max_v < v_drive_max:
            continue  # rail too high for this part
        if peak_source_min is not None and g.peak_source_a < peak_source_min:
            continue
        if half_bridge is not None and ((g.part_number in _HALF_BRIDGE) != half_bridge):
            continue
        hits.append(g)
    return sorted(hits, key=lambda g: g.peak_source_a, reverse=True)


def query_controllers(
    lib: Library,
    control_law: str | None = None,
    fsw_hz: float | None = None,
) -> list[ControllerIC]:
    """Filter controller ICs by control law and switching-frequency window.

    `fsw_hz` keeps only parts whose [fsw_min, fsw_max] contains the design's
    switching frequency. Returns sorted by fsw_max (descending).
    """
    hits = []
    for c in lib.controllers.values():
        if control_law is not None and c.control_law != control_law:
            continue
        if fsw_hz is not None and not (c.fsw_min_hz <= fsw_hz <= c.fsw_max_hz):
            continue
        hits.append(c)
    return sorted(hits, key=lambda c: c.fsw_max_hz, reverse=True)


def query_diodes(
    lib: Library,
    Vr_min: float | None = None,
    I_avg_min: float | None = None,
    Vf_max: float | None = None,
) -> list[Diode]:
    """Filter diodes. Returns sorted by Vf_25 (ascending) — lowest drop first."""
    hits = []
    for d in lib.diodes.values():
        if Vr_min is not None and d.Vr_max < Vr_min:
            continue
        if I_avg_min is not None and d.I_avg_max < I_avg_min:
            continue
        if Vf_max is not None and d.Vf_25 > Vf_max:
            continue
        hits.append(d)
    return sorted(hits, key=lambda d: d.Vf_25)
