"""Pydantic schemas for the Phase 1 component library.

Every field the downstream phases depend on is explicit here:
- Phase 2 (sizing) needs voltage/current ratings.
- Phase 3 (selection) needs ratings + package.
- Phase 5 (loss extraction) needs Qgd / V_plateau / Ciss / Rds_on on every MOSFET.
- Phase 6 (board template) needs die dimensions and thermal resistances.

All units are SI unless the field name says otherwise:
  V = volts, A = amperes, Ohm = ohms, s = seconds, F = farads, H = henries,
  degC = degrees Celsius, W = watts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class _PackageStr(BaseModel):
    """Mixin: YAML numeric package codes (1206, 7345) must stay strings."""

    package: str

    @field_validator("package", mode="before")
    @classmethod
    def _pkg(cls, v):
        return str(v)


class DatasheetRef(BaseModel):
    """Provenance for every part entry — no anonymous data."""

    url: str
    page: int | None = Field(default=None, ge=1, description="Page number the parameters were read from")
    note: str | None = None


class MOSFET(_PackageStr):
    """N-channel power MOSFET (silicon, 100-500 kHz class)."""

    part_number: str
    Vds_max: float = Field(gt=0, description="Max drain-source voltage, V")
    Rds_on: float = Field(gt=0, description="Max on-resistance at 25 degC, Ohm")
    Qg: float = Field(gt=0, description="Total gate charge at datasheet Vgs, C")
    Qgd: float = Field(gt=0, description="Gate-drain (Miller) charge, C — needed for switching-loss crossover time")
    V_plateau: float = Field(gt=0, description="Gate plateau voltage at datasheet test current, V")
    Ciss: float = Field(gt=0, description="Input capacitance, F (nF*1e-9 from datasheets)")
    Id_max: float = Field(gt=0, description="Continuous drain current at 25 degC case, A")
    package: str
    # Thermal / mechanical (Phase 6 board template + Phase 5 per-device loss)
    die_x_mm: float = Field(gt=0)
    die_y_mm: float = Field(gt=0)
    die_z_mm: float = Field(gt=0)
    R_theta_jc: float = Field(gt=0, description="Junction-to-case thermal resistance, degC/W")
    R_theta_ja: float = Field(gt=0, description="Junction-to-ambient (datasheet board), degC/W")
    Tj_max: float = Field(default=150.0, description="Max junction temperature, degC")
    datasheet: DatasheetRef

    @field_validator("Rds_on")
    @classmethod
    def rds_sane(cls, v: float) -> float:
        # 100-500 kHz silicon power MOSFETs: Rds_on in 0.5 mOhm..1 Ohm range
        if not (5e-4 <= v <= 1.0):
            raise ValueError(f"Rds_on={v} Ohm outside sane range [0.5mOhm, 1Ohm] for silicon power MOSFETs")
        return v

    @field_validator("V_plateau")
    @classmethod
    def plateau_sane(cls, v: float) -> float:
        if not (1.0 <= v <= 10.0):
            raise ValueError(f"V_plateau={v} V outside sane range [1, 10] V for silicon gate drives")
        return v

    @model_validator(mode="after")
    def charges_sane(self) -> "MOSFET":
        # Miller charge must be a fraction of total gate charge — flag datasheet typos
        if not (0.05 <= self.Qgd / self.Qg <= 0.7):
            raise ValueError(
                f"Qgd/Qg = {self.Qgd / self.Qg:.2f} outside sane [0.05, 0.7] for {self.part_number} — check datasheet"
            )
        return self


class Inductor(_PackageStr):
    """Power inductor for DC-DC output filter."""

    part_number: str
    L: float = Field(gt=0, description="Inductance, H")
    tol_percent: float = Field(default=20.0, gt=0, le=50, description="Inductance tolerance, %")
    DCR: float = Field(gt=0, description="DC resistance, Ohm")
    ESR: float = Field(default=0.0, ge=0, description="HF series resistance at f_sw, Ohm (defaults to DCR if 0)")
    Isat: float = Field(gt=0, description="Saturation current (L drops 10-30%), A")
    Irms: float = Field(gt=0, description="RMS current for 40 degC rise, A")
    f_self_res: float = Field(gt=0, description="Self-resonant frequency, Hz")
    package: str
    datasheet: DatasheetRef

    @model_validator(mode="after")
    def currents_sane(self) -> "Inductor":
        if self.Isat < self.Irms:
            # Not fatal for all topologies but always worth flagging
            raise ValueError(f"{self.part_number}: Isat < Irms — check datasheet")
        return self


class Capacitor(_PackageStr):
    """Output capacitor (MLCC or polymer/porous electrolytic)."""

    part_number: str
    C: float = Field(gt=0, description="Capacitance, F")
    tol_percent: float = Field(default=20.0, gt=0, le=50)
    ESR: float = Field(gt=0, description="Equivalent series resistance at f_sw, Ohm")
    ESL: float = Field(default=0.0, ge=0, description="Equivalent series inductance, H")
    V_rated: float = Field(gt=0, description="Rated DC voltage, V")
    Irms_max: float = Field(gt=0, description="Max ripple current, A")
    package: str
    datasheet: DatasheetRef


class LibraryFile(BaseModel):
    """Top-level wrapper of one YAML file."""

    category: str  # "mosfets" | "inductors" | "capacitors"
    parts: list[MOSFET | Inductor | Capacitor]
