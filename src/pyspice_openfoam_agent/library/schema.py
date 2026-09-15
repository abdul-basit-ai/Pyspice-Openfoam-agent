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
    manufacturer: str | None = Field(default=None, description="Manufacturer name")
    Vds_max: float = Field(gt=0, description="Max drain-source voltage, V")
    Rds_on: float = Field(gt=0, description="Max on-resistance at 25 degC, Ohm")
    # Rds_on temperature coefficient (electro-thermal convergence, Phase 9):
    # normalized Rds_on(T) = Rds_on_25 * (1 + tempco_ppm/1e6 * (T - 25)).
    # Typical silicon power MOSFETs: +4000..+8000 ppm/K (positive tempco).
    Rds_on_tempco_ppm: float = Field(
        default=6000.0, description="Rds_on temperature coefficient, ppm/K (positive for silicon)")
    Qg: float = Field(gt=0, description="Total gate charge at datasheet Vgs, C")
    Qgd: float = Field(gt=0, description="Gate-drain (Miller) charge, C — needed for switching-loss crossover time")
    V_plateau: float = Field(gt=0, description="Gate plateau voltage at datasheet test current, V")
    Ciss: float = Field(gt=0, description="Input capacitance, F (nF*1e-9 from datasheets)")
    Coss: float | None = Field(default=None, gt=0, description="Output capacitance, F (optional)")
    Crss: float | None = Field(default=None, gt=0, description="Reverse-transfer capacitance, F (optional)")
    # Synchronous-rectifier loss terms (task: HS hard-switch vs LS freewheel):
    # reverse-recovery charge of the LS body diode, and its forward voltage.
    Qrr: float | None = Field(default=None, gt=0,
                              description="LS body-diode reverse-recovery charge, C (optional; high for slow diodes)")
    V_F: float = Field(default=0.7, gt=0.1, lt=2.0,
                       description="LS body-diode forward voltage during dead time, V")
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

    def rds_on_at(self, tj_c: float) -> float:
        """Temperature-corrected Rds_on for the electro-thermal loop."""
        return self.Rds_on * (1.0 + self.Rds_on_tempco_ppm / 1e6 * (tj_c - 25.0))

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
    manufacturer: str | None = Field(default=None, description="Manufacturer name")
    L: float = Field(gt=0, description="Inductance, H")
    tol_percent: float = Field(default=20.0, gt=0, le=50, description="Inductance tolerance, %")
    DCR: float = Field(gt=0, description="DC resistance, Ohm")
    # Copper tempco (~0.39%/K for copper) — used by the electro-thermal loop
    DCR_tempco_ppm: float = Field(default=3900.0, description="DCR temperature coefficient, ppm/K")
    ESR: float = Field(default=0.0, ge=0, description="HF series resistance at f_sw, Ohm (defaults to DCR if 0)")
    # Core-loss model (electro-thermal convergence, Phase 9): Steinmetz params
    # P_core = k * f_sw^alpha * (delta_B)^beta, W. None = core loss neglected.
    core_loss_k: float | None = Field(default=None, gt=0, description="Steinmetz k (W)")
    core_loss_alpha: float | None = Field(default=None, gt=0, description="Steinmetz frequency exponent")
    core_loss_beta: float | None = Field(default=None, gt=0, description="Steinmetz flux-swing exponent")
    Isat: float = Field(gt=0, description="Saturation current (L drops 10-30%), A")
    Irms: float = Field(gt=0, description="RMS current for 40 degC rise, A")
    f_self_res: float = Field(gt=0, description="Self-resonant frequency, Hz")
    package: str
    datasheet: DatasheetRef

    def dcr_at(self, t_c: float) -> float:
        """Temperature-corrected DCR for the electro-thermal loop."""
        return self.DCR * (1.0 + self.DCR_tempco_ppm / 1e6 * (t_c - 25.0))

    @model_validator(mode="after")
    def currents_sane(self) -> "Inductor":
        if self.Isat < self.Irms:
            # Not fatal for all topologies but always worth flagging
            raise ValueError(f"{self.part_number}: Isat < Irms — check datasheet")
        return self


class Capacitor(_PackageStr):
    """Output capacitor (MLCC or polymer/porous electrolytic)."""

    part_number: str
    manufacturer: str | None = Field(default=None, description="Manufacturer name")
    C: float = Field(gt=0, description="Capacitance, F")
    tol_percent: float = Field(default=20.0, gt=0, le=50)
    ESR: float = Field(gt=0, description="Equivalent series resistance at f_sw, Ohm")
    ESL: float = Field(default=0.0, ge=0, description="Equivalent series inductance, H")
    V_rated: float = Field(gt=0, description="Rated DC voltage, V")
    Irms_max: float = Field(gt=0, description="Max ripple current, A")
    package: str
    datasheet: DatasheetRef


class GateDriver(_PackageStr):
    """Gate driver IC (Phase 6 control-loop / Phase 5 schematic generation)."""

    part_number: str
    manufacturer: str | None = Field(default=None)
    drive_voltage_min_v: float = Field(gt=0, description="Min drive output voltage, V")
    drive_voltage_max_v: float = Field(gt=0, description="Max drive output voltage, V")
    peak_source_a: float = Field(gt=0, description="Peak source current, A")
    peak_sink_a: float = Field(gt=0, description="Peak sink current, A")
    propagation_delay_ns: float = Field(gt=0, description="Propagation delay, ns")
    package: str
    R_theta_ja: float | None = Field(default=None, gt=0, description="degC/W")
    Tj_max: float = Field(default=125.0, description="Max junction temperature, degC")
    datasheet: DatasheetRef


class ControllerIC(_PackageStr):
    """PWM controller IC (Phase 6 control law)."""

    part_number: str
    manufacturer: str | None = Field(default=None)
    control_law: str = Field(description="voltage | peak-current | other")
    fsw_min_hz: float = Field(gt=0, description="Min switching frequency, Hz")
    fsw_max_hz: float = Field(gt=0, description="Max switching frequency, Hz")
    v_ref: float = Field(gt=0, description="Reference voltage, V")
    v_ref_tol_percent: float = Field(default=1.0, gt=0, description="Reference tolerance, %")
    package: str
    Tj_max: float = Field(default=125.0)
    datasheet: DatasheetRef


class Diode(_PackageStr):
    """Diode / synchronous rectifier (Vf + tempco for electro-thermal loop)."""

    part_number: str
    manufacturer: str | None = Field(default=None)
    Vf_25: float = Field(gt=0, description="Forward voltage at 25 degC at rated I, V")
    Vf_tempco_mv_per_k: float = Field(default=-2.0, description="Vf tempco, mV/K (negative for silicon)")
    I_avg_max: float = Field(gt=0, description="Max average forward current, A")
    Vr_max: float = Field(gt=0, description="Max reverse voltage, V")
    trr_ns: float | None = Field(default=None, ge=0, description="Reverse recovery time, ns (None = Schottky)")
    package: str
    R_theta_ja: float | None = Field(default=None, gt=0, description="degC/W")
    Tj_max: float = Field(default=150.0)
    datasheet: DatasheetRef

    def vf_at(self, tj_c: float) -> float:
        """Temperature-corrected Vf for the electro-thermal loop."""
        return self.Vf_25 + self.Vf_tempco_mv_per_k / 1e3 * (tj_c - 25.0)


class LibraryFile(BaseModel):
    """Top-level wrapper of one YAML file."""

    category: str  # mosfets | inductors | capacitors | gate_drivers | controllers | diodes
    parts: list[MOSFET | Inductor | Capacitor | GateDriver | ControllerIC | Diode]
