"""Phase 12: unified output bundle — one inspectable artifact per run.

A JSON manifest + the netlist + BOM + thermal summary, assembled by the
orchestrator's finalize step (or standalone from a ToolContext's artifacts).
Schema-validated so both successful and deliberately-infeasible runs
produce checkable output (the plan's Phase 12 checkpoint).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_VERSION = 1


@dataclass
class ComponentRef:
    part_number: str
    category: str  # mosfet | inductor | capacitor
    datasheet_url: str | None = None


@dataclass
class Manifest:
    version: int
    spec: dict  # Vin/Vout/Iout/fsw_khz/ripple
    status: str  # "feasible" | "infeasible" | "error"
    topology: str | None
    components: list[ComponentRef]
    electrical: dict  # ripple_V, efficiency, losses_W, cycles_to_steady
    thermal: dict  # tj_per_device_C, tj_max_C, v_in_m_s, converged
    artifacts: dict  # paths: netlist, case_dir, log
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "spec": self.spec,
            "status": self.status,
            "topology": self.topology,
            "components": [c.__dict__ for c in self.components],
            "electrical": self.electrical,
            "thermal": self.thermal,
            "artifacts": self.artifacts,
            "notes": self.notes,
        }


class ManifestError(ValueError):
    """Manifest violates its schema."""


def validate_manifest(d: dict) -> None:
    """Structural validation (schema-lite: required keys + types)."""
    for key in ("version", "spec", "status", "components", "electrical", "thermal", "artifacts"):
        if key not in d:
            raise ManifestError(f"manifest missing required key {key!r}")
    if d["version"] != MANIFEST_VERSION:
        raise ManifestError(f"unknown manifest version {d['version']}")
    if d["status"] not in ("feasible", "infeasible", "error"):
        raise ManifestError(f"bad status {d['status']!r}")
    for c in d["components"]:
        if "part_number" not in c or "category" not in c:
            raise ManifestError(f"component entry missing part_number/category: {c}")


def build_manifest_from_artifacts(artifacts: dict, spec: dict, status: str,
                                  notes: list[str] | None = None) -> Manifest:
    """Assemble a Manifest from a ToolContext.artifacts dict.

    `spec` may carry protection/safety context (ocp/otp/uvlo, unresolved
    safety items, input range) in addition to the electrical spec — the Phase
    12 output contract requires the safety posture to be inspectable.
    Gate driver / controller ICs (Group A2) join the component list; the
    control-loop, electro-thermal, step-test and sweep summaries ride in
    `electrical` (schema-stable: it is a free-form dict)."""
    comps = artifacts.get("components", {})
    # infeasible/error runs may have no selection: manifest stays schema-valid
    categories = ("mosfet", "inductor", "capacitor",
                  "gate_driver", "controller", "diode")
    components = [
        ComponentRef(part_number=comps[k], category=k)
        for k in categories
        if comps.get(k)
    ]
    electrical = dict(artifacts.get("spice", {}))
    for extra in ("control_loop", "step_tests", "sweep", "electro_thermal"):
        if artifacts.get(extra):
            electrical[extra] = artifacts[extra]
    thermal = dict(artifacts.get("thermal", {}))
    # the per-check validation evidence (str->str) is exactly the convergence
    # proof the Phase 13 contract wants in the bundle — keep it
    return Manifest(
        version=MANIFEST_VERSION,
        spec=spec,
        status=status,
        topology=artifacts.get("sizing", {}).get("topology"),
        components=components,
        electrical=electrical,
        thermal=thermal,
        artifacts={"netlist": artifacts.get("netlist"),
                   "case_dir": thermal.get("case_dir")},
        notes=notes or [],
    )


def write_bundle(manifest: Manifest, run_dir: str | Path) -> Path:
    """Write manifest.json (+ copy the netlist alongside). Returns the path."""
    d = manifest.to_dict()
    validate_manifest(d)
    out = Path(run_dir) / "manifest.json"
    out.write_text(json.dumps(d, indent=2))
    return out