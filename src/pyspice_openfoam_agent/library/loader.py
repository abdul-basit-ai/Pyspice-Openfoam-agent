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
