"""Phase 4: the unified Design representation — the central source of truth.

The goals demand: everything (schematic, netlist, BOM, PCB geometry,
OpenFOAM case, reports) is generated from ONE object, and separate
representations must never silently diverge. This module is that object.

Structure (per the goals' Phase 4 tree):

Design
 ├── requirements        Spec (user constraints + optimization objectives)
 ├── topology            chosen converter family + control law
 ├── parameters          sizing results (D, L_min, C_min, currents...)
 ├── components          selected real parts + connections
 ├── constraints         hard limits (Tj, ripple, protection)
 ├── optimization_objectives
 ├── schematic           path to rendered schematic + generator params
 ├── electrical_model    netlist path + sim config
 ├── physical_model      board geometry params
 └── simulation_configuration

Plus `schema_version` (goals gap #6): old serialized designs in memory are
either migrated or marked as prior-version — never silently reinterpreted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2  # v2: initial unified Design (goals gap #6)


class DesignError(ValueError):
    """Design object invariant violated."""


@dataclass
class Requirements:
    """User constraints: hard limits + optimization objectives (P2 output).

    Vin is the DESIGN point the sizing engine uses. When the user specifies
    an input RANGE ("12-36 V"), the parser additionally records vin_min /
    vin_max and designs at the worst-case VOLTAGE-STRESS endpoint (max) with
    a loud assumption — the range is never silently collapsed (additive v2
    schema fields, default None; old serialized designs load unchanged)."""

    Vin: float = 0.0
    Vout: float = 0.0
    Iout: float = 0.0
    fsw_khz: float = 500.0
    ripple_v: float = 0.01
    ripple_ratio: float = 0.30
    efficiency_target: float | None = None
    tj_max_c: float = 150.0
    # input range endpoints when the spec gave one (None = fixed Vin)
    vin_min: float | None = None
    vin_max: float | None = None
    # protection (P2, goals gap: safety-relevant — ask if unspecified)
    ocp_a: float | None = None
    otp_c: float | None = None
    uvlo_v: float | None = None
    unresolved_safety_items: list[str] = field(default_factory=list)

    def as_spec_kwargs(self) -> dict:
        return {
            "Vin": self.Vin, "Vout": self.Vout, "Iout": self.Iout,
            "fsw": self.fsw_khz * 1e3, "ripple_ratio": self.ripple_ratio,
            "Vripple": self.ripple_v,
        }


@dataclass
class TopologyChoice:
    name: str = ""  # buck | boost | buck_boost (set during P3)
    control_law: str = "voltage"  # voltage | peak-current
    rationale: str = ""  # P3's explanation contract (why this topology)
    alternatives_considered: list[str] = field(default_factory=list)


@dataclass
class DesignParameters:
    """Analytical sizing results (P3)."""

    duty_cycle: float | None = None
    L_min_h: float | None = None
    C_min_f: float | None = None
    di_pp_a: float | None = None
    i_l_avg_a: float | None = None
    i_peak_a: float | None = None
    i_rms_sw_a: float | None = None


@dataclass
class ComponentRef:
    category: str  # mosfet | inductor | capacitor | gate_driver | controller | diode
    part_number: str
    datasheet_url: str | None = None


@dataclass
class DesignComponents:
    mosfet: ComponentRef | None = None
    inductor: ComponentRef | None = None
    capacitor: ComponentRef | None = None
    gate_driver: ComponentRef | None = None
    controller: ComponentRef | None = None
    diode: ComponentRef | None = None


@dataclass
class DesignConstraints:
    tj_limit_c: float = 150.0
    vds_derating: float = 1.2  # switch stress = Vin * this
    id_derating: float = 1.3
    screening_enabled: bool = True  # P6 gate


@dataclass
class SimulationConfiguration:
    spice_max_cycles: int = 800
    cht_end_time: int = 100
    cht_timeout_s: int = 600
    cht_fidelity: str = "balanced"  # fast | balanced | high (P10 presets)
    cht_v_in_m_s: float = 1.0
    ambient_k: float = 300.0


@dataclass
class Design:
    """The single source of truth. Everything else is generated from it."""

    schema_version: int = SCHEMA_VERSION
    requirements: Requirements = field(default_factory=Requirements)
    topology: TopologyChoice = field(default_factory=TopologyChoice)
    parameters: DesignParameters = field(default_factory=DesignParameters)
    components: DesignComponents = field(default_factory=DesignComponents)
    constraints: DesignConstraints = field(default_factory=DesignConstraints)
    simulation: SimulationConfiguration = field(default_factory=SimulationConfiguration)
    # generated-artifact paths (outputs of later phases, referencing this object)
    netlist_path: str | None = None
    schematic_png: str | None = None
    thermal_case_dir: str | None = None
    tj_png: str | None = None
    provenance: list[dict] = field(default_factory=list)  # P18 goals: decision trail

    # ---------- validation ----------

    def validate(self) -> list[str]:
        problems = []
        r = self.requirements
        if r.Vout >= r.Vin and self.topology.name == "buck":
            problems.append("buck topology requires Vout < Vin")
        if r.Vout <= r.Vin and self.topology.name == "boost":
            problems.append("boost topology requires Vin < Vout")
        if r.fsw_khz < 50 or r.fsw_khz > 1000:
            problems.append(f"f_sw {r.fsw_khz} kHz outside supported band [50, 1000]")
        if self.components.mosfet is None and self.netlist_path:
            problems.append("netlist exists but no MOSFET selected — representations diverged")
        return problems

    # ---------- serialization (P4 checkpoint: serialize, reload, regenerate) ----------

    def to_json(self) -> str:
        return json.dumps({"schema_version": self.schema_version, "design": asdict(self)},
                          indent=2, default=str)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Design":
        raw = json.loads(Path(path).read_text())
        version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            # goals gap #6: never silently reinterpret old schemas
            raise DesignError(
                f"design file schema_version={version}, current={SCHEMA_VERSION}. "
                "Migrate explicitly or load with load_prior_version()."
            )
        return cls._from_dict(raw["design"])

    @classmethod
    def load_prior_version(cls, path: str | Path) -> tuple["Design", int]:
        """Load a prior-version design, explicitly marked (goals gap #6)."""
        raw = json.loads(Path(path).read_text())
        version = raw.get("schema_version", 0)
        d = cls._from_dict(raw.get("design", raw))
        d.provenance.append({
            "event": "loaded_prior_version",
            "file_schema_version": version,
            "current_schema_version": SCHEMA_VERSION,
        })
        return d, version

    @classmethod
    def _from_dict(cls, d: dict) -> "Design":
        """Rebuild from asdict() output. Unknown keys are dropped loudly."""
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise DesignError(f"unknown Design fields {sorted(unknown)} — "
                              "file is from a NEWER schema; migrate explicitly")
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            requirements=Requirements(**d.get("requirements", {})),
            topology=TopologyChoice(**{k: v for k, v in d.get("topology", {}).items()
                                       if k in TopologyChoice.__dataclass_fields__}),
            parameters=DesignParameters(**{k: v for k, v in d.get("parameters", {}).items()
                                           if k in DesignParameters.__dataclass_fields__}),
            components=DesignComponents(**{
                k: (ComponentRef(**v) if isinstance(v, dict) else v)
                for k, v in d.get("components", {}).items()
                if k in DesignComponents.__dataclass_fields__ and v is not None
            }),
            constraints=DesignConstraints(**d.get("constraints", {})),
            simulation=SimulationConfiguration(**d.get("simulation", {})),
            netlist_path=d.get("netlist_path"),
            schematic_png=d.get("schematic_png"),
            thermal_case_dir=d.get("thermal_case_dir"),
            tj_png=d.get("tj_png"),
            provenance=d.get("provenance", []),
        )

    # ---------- provenance (P18 goals: explain decisions) ----------

    def record_decision(self, decision: str, reason: str, constraints_considered: str,
                        alternatives: str, assumptions: str,
                        validation_status: str = "unvalidated") -> None:
        """The goals' decision-explanation contract as data."""
        self.provenance.append({
            "decision": decision, "reason": reason,
            "constraints_considered": constraints_considered,
            "alternatives_considered": alternatives,
            "assumptions": assumptions,
            "validation_status": validation_status,
        })