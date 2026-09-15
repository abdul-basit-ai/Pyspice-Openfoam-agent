"""Phase 3: Emit a valid ngspice .cir netlist from a sizing result + selected parts.

Design choice -- ideal/behavioral switches, not detailed MOSFET SPICE models:
the Phase 1 library carries datasheet ratings (Vds_max, Rds_on, Qg, Qgd...),
not the BSIM/Level-1 model-card parameters (VTO, KP, LAMBDA...) a real
`.model NMOS NMOS(...)` needs, and manufacturers don't publish those for
free. This project's loss accounting is also deliberately split that way:
Phase 4/5 extract conduction loss from the *waveform* (I^2*R integration
using the real Rds_on) and switching loss from an *explicit crossover-time
model* using Qgd/V_plateau -- not from watching a detailed MOSFET model
switch in SPICE. So the netlist only needs each switch's ON resistance to be
correct; ngspice's built-in voltage-controlled switch (`S` device, `SW`
model) driven by an ideal PWM gate source gives exactly that, with zero
invented model parameters. Inductor DCR and capacitor ESR are modeled as
explicit series R elements next to ideal L/C, for the same reason: it's the
real part's loss-relevant parasitic, not a lumped guess (the capacitor ESL
is deliberately NOT emitted -- see _cap_branch for why it poisons the rig).

Topology coverage: buck and boost use one high/low switch pair (synchronous
rectification -- one selected MOSFET part number used for both switches,
standard practice at the Rds_on this library targets) with complementary
PWM gate drives, explicit dead time, and antiparallel body-diode clamps.
buck_boost is the 4-switch NON-INVERTING topology at +|Vout| (HS/LS_A/SYNC/
LS_B; see _build_buck_boost) -- the classic 2-switch inverting buck-boost
cannot source a positive output rail in this switch arrangement (confirmed
empirically; the stale "output at magnitude" note is retired).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from pyspice_openfoam_agent.netlist.selector import SelectedComponents
from pyspice_openfoam_agent.sizing.engine import Spec, SizingResult

_GATE_RISE_FALL = 1e-9  # 1 ns gate edge time
# Gate drive the rig assumes for Miller-plateau scaling (single source of
# truth; spice/losses.py imports these for its crossover model so the two
# can never drift apart).
GATE_DRIVE_V = 5.0
GATE_DRIVE_R = 2.0
# Deterministic non-overlap dead time. MUST be > 2*gate edge time so the two
# switches are never simultaneously on (synchronous shoot-through guard).
# Scaled from the library's Miller crossover time when available, defaulting to
# 30 ns (a conservative silicon-part value at 100-500 kHz).
_DEAD_TIME_DEFAULT_S = 30e-9


def dead_time_for(mosfet) -> float:
    """Non-overlap gap for a MOSFET: 2x the Miller crossover time at the
    rig's gate drive (turn-off plus margin — a value a real gate driver would
    enforce), floored at the conservative 30 ns silicon default. The floor
    also guarantees td > 2x gate edge, which is what kills shoot-through
    between the ideal switches (they switch in ~1 ns)."""
    qgd = getattr(mosfet, "Qgd", None)
    vplat = getattr(mosfet, "V_plateau", None)
    if qgd and vplat and vplat < GATE_DRIVE_V:
        i_gate = (GATE_DRIVE_V - vplat) / GATE_DRIVE_R
        t_cross = qgd / i_gate
        return max(_DEAD_TIME_DEFAULT_S, 2.0 * t_cross)
    return _DEAD_TIME_DEFAULT_S


def _dead_time(sel) -> float:
    try:
        return dead_time_for(getattr(sel, "mosfet", sel))
    except Exception:
        return _DEAD_TIME_DEFAULT_S


# Soft-start ramp duration: at least 300 switching periods (task spec) and a
# sane floor. Exponential/LTI recommendation ~1-2 ms; we use 300*Tsw which at
# 300 kHz = 1 ms, at 500 kHz = 0.6 ms — always sub-ms so the steady-state
# detector still sees a settled tail inside a bounded cycle budget.
def _soft_start_s(Tsw: float) -> float:
    """Soft-start ramp length: >= 300 switching periods (task spec)."""
    return 300.0 * Tsw


class NetlistLintError(RuntimeError):
    """ngspice -b reported an error while parsing a generated netlist."""


def _fmt(x: float) -> str:
    """Consistent scientific-notation formatting for netlist parameter values."""
    return f"{x:.6e}"


def _cap_branch(node_in: str, node_out: str, C: float, ESR: float, ESL: float, ref: str) -> str:
    """Cout -- Resr -- node_out. The library's ESL (a few nH) is deliberately
    NOT emitted: with the rig's ideal zero-transition-time switches and
    zero-junction-cap diodes, commutation steps dI/dt are unbounded, and
    L_esl * dI/dt produces ~100 V non-physical spikes on the output node
    every cycle (empirically +/-40 V on the boost, where the switch ties
    directly to out). At 100 kHz-1 MHz the ESL reactance is milliohms -- it
    carries no real information here but destroys the rig (audit finding).
    """
    return f"C{ref} {node_in} {ref}_esr {_fmt(C)}\nResr{ref} {ref}_esr {node_out} {_fmt(ESR)}"


def _switch_model_name(tag: str) -> str:
    """Single source of truth for a switch's .model name, so the device line
    that references it (written separately in each topology builder) and the
    .model line generated here can never drift apart."""
    return f"{tag.upper()}_MOD"


def _body_diode_models() -> str:
    """Diode MODEL card for body-diode clamps (real D diode).

    With ideal (voltage-controlled) switches the dead-time interval has NO
    current path (Roff=1e9 in both directions), so the inductor forces the
    switch node to ring / flip sign unless the synchronous rectifier's body
    diode freewheels it — exactly what a real MOSFET's body diode does during
    dead time. Each topology builder wires `D<tag> <anode> <cathode> Dbody`
    antiparallel to a switch (uniquely named); this emits the single shared
    .model. During dead time the LS diode conducts source->drain (cathode to
    switch node for a buck) at ~0.7 V, clamping the node and carrying the
    inductor current.
    """
    return ".model Dbody D(Is=1e-9 N=1.0 Cjo=0.0 Vj=0.7 M=0.33)"


def _switch_pair(hs_name: str, ls_name: str, Rds_on: float, D: float, Tsw: float,
                 td_s: float | None = None) -> str:
    """Complementary non-overlap dead-time gate pair (.models + PWM drives).

    Conducting windows (both gates written as PULSE(0 5 ...)):
      HS conducts  [0,  D*Tsw - td]        (turns off td before the ideal edge)
      LS conducts  [D*Tsw + td, Tsw - td]  (on td after HS off, off td before
                                            the period wraps back to HS on)
    so the two switches are separated by a td guaranteed-open gap at BOTH
    commutation boundaries — including the period wrap (LS off / HS on),
    which a plain complementary pair would otherwise switch with exactly 0 ns
    of gap (audit finding: 4 kA shoot-through pulses at Ron ~ 1.4 mOhm).
    The device (S...) lines are written by the caller per topology.

    DC value note: each gate source carries an explicit DC operating value so
    an `.op` solve reproduces the designed duty cycle (HS=5 V ON, LS=0 V OFF)
    instead of the all-off state ngspice assumes with no DC value.
    """
    td = td_s if td_s is not None else _DEAD_TIME_DEFAULT_S
    t_hs_off = D * Tsw - td                        # HS high until here
    t_ls_on = D * Tsw + td                         # LS high from here
    t_ls_pw = Tsw - td - t_ls_on                   # LS high width (ends td before Tsw)
    # Extreme duty cycles: a pulse narrower than ~2 gate edges cannot be
    # rendered by ngspice (it silently stretches TR/TF and changes the duty).
    # Fail loudly here instead of emitting a netlist that simulates the wrong
    # converter (audit finding).
    for label, pw in ((f"HS on-width (D*Tsw - td = {t_hs_off:.3e} s)", t_hs_off),
                      (f"LS on-width ({t_ls_pw:.3e} s)", t_ls_pw)):
        if pw < 2.0 * _GATE_RISE_FALL:
            raise ValueError(
                f"dead-time/pulse-width collapse: {label} < 2x gate edge "
                f"({2.0 * _GATE_RISE_FALL:.0e} s). Duty D={D:.4f} is too extreme "
                f"for td={td * 1e9:.0f} ns at fsw={1.0 / Tsw / 1e3:.0f} kHz."
            )
    return "\n".join(
        [
            f".model {_switch_model_name(hs_name)} SW(Ron={_fmt(Rds_on)} Roff=1e9 Vt=2.5 Vh=0.1)",
            f".model {_switch_model_name(ls_name)} SW(Ron={_fmt(Rds_on)} Roff=1e9 Vt=2.5 Vh=0.1)",
            f"Vgate_{hs_name} gate_{hs_name} 0 DC 5 PULSE(0 5 0 {_fmt(_GATE_RISE_FALL)} "
            f"{_fmt(_GATE_RISE_FALL)} {_fmt(t_hs_off)} {_fmt(Tsw)})",
            f"Vgate_{ls_name} gate_{ls_name} 0 DC 0 PULSE(0 5 {_fmt(t_ls_on)} {_fmt(_GATE_RISE_FALL)} "
            f"{_fmt(_GATE_RISE_FALL)} {_fmt(t_ls_pw)} {_fmt(Tsw)})",
        ]
    )


def _build_buck(spec: Spec, sizing: SizingResult, sel: SelectedComponents,
                charge_trim: float = 1.0) -> str:
    Tsw = 1.0 / spec.fsw
    Rload = spec.Vout / spec.Iout
    td = _dead_time(sel)
    # Soft-start: ramp the input rail 0 -> Vin over t_ss (>=300*Tsw) so the
    # LC output tank is charged gently, eliminating startup overshoot (task
    # bug #3). Open-loop fixed-D: ramping V_in is the correct gentle excitation.
    t_ss = _soft_start_s(Tsw)
    D_cmd = sizing.D * charge_trim
    lines = [
        f"Buck converter - Phase 3 synthesized netlist ({sel.mosfet.part_number}, "
        f"{sel.inductor.part_number}, {sel.capacitor.part_number})",
        f"* Soft-start: input PWL 0 0, t_ss {t_ss:.3g} -> {_fmt(spec.Vin)} V "
        f"({int(t_ss/Tsw)} switching periods)",
        f"Vin in 0 DC {_fmt(spec.Vin)} PWL(0 0 {_fmt(t_ss)} {_fmt(spec.Vin)})",
        "",
        "* High-side (control) / low-side (sync rect) switch pair",
        f"Shs in sw gate_hs 0 {_switch_model_name('hs')}",
        f"Sls sw 0 gate_ls 0 {_switch_model_name('ls')}",
        "* Body-diode clamps (freewheel dead-time current; LS clamps sw >= -0.7 V)",
        "Dhs sw in Dbody",
        "Dls 0 sw Dbody",
        _body_diode_models(),
        _switch_pair("hs", "ls", sel.mosfet.Rds_on, D_cmd, Tsw, td_s=td),
        "",
        "* Output filter inductor with real DCR in series",
        f"Lout sw lx {_fmt(sel.inductor.L)}",
        f"Rdcr lx out {_fmt(sel.inductor.DCR)}",
        "",
        "* Output capacitor with real ESR (+ ESL) in series",
        _cap_branch("out", "0", sel.capacitor.C, sel.capacitor.ESR, sel.capacitor.ESL, "out"),
        "",
        f"Rload out 0 {_fmt(Rload)}",
        "",
        f"* duty servo trim {charge_trim:.4f} -> commanded D {D_cmd:.4f} "
        f"(design D {sizing.D:.4f})",
        ".op",
        ".end",
    ]
    return "\n".join(lines) + "\n"


def _build_boost(spec: Spec, sizing: SizingResult, sel: SelectedComponents,
                 charge_trim: float = 1.0) -> str:
    Tsw = 1.0 / spec.fsw
    Rload = spec.Vout / spec.Iout
    td = _dead_time(sel)
    t_ss = _soft_start_s(Tsw)
    D_cmd = sizing.D * charge_trim
    lines = [
        f"Boost converter - Phase 3 synthesized netlist ({sel.mosfet.part_number}, "
        f"{sel.inductor.part_number}, {sel.capacitor.part_number})",
        f"* Soft-start: input PWL 0 0, t_ss {t_ss:.3g} -> {_fmt(spec.Vin)} V "
        f"({int(t_ss/Tsw)} switching periods)",
        f"Vin in0 0 DC {_fmt(spec.Vin)} PWL(0 0 {_fmt(t_ss)} {_fmt(spec.Vin)})",
        "",
        "* Input inductor with real DCR in series",
        f"Lin in0 lx {_fmt(sel.inductor.L)}",
        f"Rdcr lx sw {_fmt(sel.inductor.DCR)}",
        "",
        "* Control switch (to ground, charges L) / sync switch (to output, discharges L)",
        f"Sctrl sw 0 gate_ctrl 0 {_switch_model_name('ctrl')}",
        f"Ssync sw out gate_sync 0 {_switch_model_name('sync')}",
        "* Body-diode clamps (freewheel dead-time current; sync diode clamps sw to >= -0.7 V)",
        "Dctrl 0 sw Dbody",
        "Dsync sw out Dbody",
        _body_diode_models(),
        _switch_pair("ctrl", "sync", sel.mosfet.Rds_on, D_cmd, Tsw, td_s=td),
        "",
        "* Output capacitor with real ESR (+ ESL) in series",
        _cap_branch("out", "0", sel.capacitor.C, sel.capacitor.ESR, sel.capacitor.ESL, "out"),
        "",
        f"Rload out 0 {_fmt(Rload)}",
        "",
        f"* duty servo trim {charge_trim:.4f} -> commanded D {D_cmd:.4f} "
        f"(design D {sizing.D:.4f})",
        ".op",
        ".end",
    ]
    return "\n".join(lines) + "\n"


def _build_buck_boost(spec: Spec, sizing: SizingResult, sel: SelectedComponents,
                      charge_trim: float = 1.0) -> str:
    """Non-inverting (4-switch) buck-boost, output at +|Vout|.

    Topology correction (audit finding): the classic 2-switch INVERTING
    buck-boost cannot be re-referenced to a positive output with just two
    switches -- with L tied to ground, the discharge phase sources current
    FROM the output through the sync switch into L, draining C_out instead of
    charging it (empirically confirmed: v(out) stays at 0 V after 300 us of
    PWM). The correct magnitude-mode circuit is the 4-switch non-inverting
    buck-boost:

        Vin --HS-- sw_a --L-- sw_b --SYNC-- out
        sw_a --LS_A-- gnd     sw_b --LS_B-- gnd

    Gate phasing (two phase pairs, per the standard 4-switch buck-boost):
      - charge phase, D*Tsw: HS on, LS_B on
        (Vin -> HS -> L -> LS_B -> gnd: inductor charges from Vin)
      - discharge phase, (1-D)*Tsw: LS_A on, SYNC on
        (gnd -> LS_A -> L -> SYNC -> out: inductor delivers to C_out at +|Vout|)
    In buck mode (Vin >> Vout) this reduces to HS/SYNC complementary PWM with
    LS_A as synchronous rectifier only when LS_B is held OFF -- the pure
    buck-boost region phasing above is what this netlist emits, which is the
    correct behavior when D comes from the buck-boost sizing equation
    D = Vout/(Vin+Vout).
    """
    Tsw = 1.0 / spec.fsw
    Rload = spec.Vout / spec.Iout
    td = _dead_time(sel)
    t_ss = _soft_start_s(Tsw)
    dt_charge = sizing.D * charge_trim * Tsw
    D_cmd = sizing.D * charge_trim
    dt_discharge = (1.0 - sizing.D * charge_trim) * Tsw
    # Dead-time staggered phase boundaries: charge ends td early, discharge
    # starts td late (and vice-versa), so the two pairs never overlap.
    t_charge_on = dt_charge - td
    t_discharge_on = dt_charge + td
    t_discharge_end = Tsw - td
    # Same extreme-duty guard as _switch_pair: a gate pulse narrower than two
    # edge times cannot be rendered by ngspice and silently changes the duty.
    for label, pw in ((f"charge-phase width ({t_charge_on:.3e} s)", t_charge_on),
                      (f"discharge-phase width ({t_discharge_end - t_discharge_on:.3e} s)",
                       t_discharge_end - t_discharge_on)):
        if pw < 2.0 * _GATE_RISE_FALL:
            raise ValueError(
                f"dead-time/pulse-width collapse in buck_boost: {label} < 2x gate "
                f"edge. Duty D={sizing.D:.4f} too extreme for td={td * 1e9:.0f} ns "
                f"at fsw={spec.fsw / 1e3:.0f} kHz."
            )
    mod = {tag: _switch_model_name(tag) for tag in ("hs", "ls_a", "ls_b", "sync")}
    sw_model = lambda tag: (  # noqa: E731
        f".model {mod[tag]} SW(Ron={_fmt(sel.mosfet.Rds_on)} Roff=1e9 Vt=2.5 Vh=0.1)"
    )
    lines = [
        f"Buck-boost converter - Phase 3 synthesized netlist ({sel.mosfet.part_number}, "
        f"{sel.inductor.part_number}, {sel.capacitor.part_number})",
        "* 4-switch non-inverting topology at +|Vout| (see docstring correction note)",
        f"* Soft-start: input PWL 0 0, t_ss {t_ss:.3g} -> {_fmt(spec.Vin)} V "
        f"({int(t_ss/Tsw)} switching periods)",
        f"Vin in 0 DC {_fmt(spec.Vin)} PWL(0 0 {_fmt(t_ss)} {_fmt(spec.Vin)})",
        "",
        "* Switches: HS (in->sw_a), LS_A (sw_a->gnd), SYNC (sw_b->out), LS_B (sw_b->gnd)",
        f"Shs in sw_a gate_hs 0 {mod['hs']}",
        f"Sls_a sw_a 0 gate_ls_a 0 {mod['ls_a']}",
        f"Ssync sw_b out gate_sync 0 {mod['sync']}",
        f"Sls_b sw_b 0 gate_ls_b 0 {mod['ls_b']}",
        "* Body-diode clamps (freewheel dead-time current on both switch nodes)",
        "Dhs sw_a in Dbody",
        "Dls_a 0 sw_a Dbody",
        "Dsync sw_b out Dbody",
        "Dls_b 0 sw_b Dbody",
        _body_diode_models(),
        "",
        "* Floating inductor with real DCR in series (sw_a -- L -- Rdcr -- sw_b)",
        f"Lout sw_a lx {_fmt(sel.inductor.L)}",
        f"Rdcr lx sw_b {_fmt(sel.inductor.DCR)}",
        "",
        "* Output capacitor with real ESR (+ ESL) in series",
        _cap_branch("out", "0", sel.capacitor.C, sel.capacitor.ESR, sel.capacitor.ESL, "out"),
        "",
        f"Rload out 0 {_fmt(Rload)}",
        "",
        "* Gate phasing w/ dead time: charge = HS+LS_B on [0, D*Tsw-td]; "
        "discharge = LS_A+SYNC on [D*Tsw+td, Tsw-td]",
        sw_model("hs"),
        sw_model("ls_a"),
        sw_model("ls_b"),
        sw_model("sync"),
        f"Vgate_hs gate_hs 0 DC 5 PULSE(0 5 0 {_fmt(_GATE_RISE_FALL)} "
        f"{_fmt(_GATE_RISE_FALL)} {_fmt(t_charge_on)} {_fmt(Tsw)})",
        f"Vgate_ls_b gate_ls_b 0 DC 5 PULSE(0 5 0 {_fmt(_GATE_RISE_FALL)} "
        f"{_fmt(_GATE_RISE_FALL)} {_fmt(t_charge_on)} {_fmt(Tsw)})",
        f"Vgate_ls_a gate_ls_a 0 DC 0 PULSE(0 5 {_fmt(t_discharge_on)} {_fmt(_GATE_RISE_FALL)} "
        f"{_fmt(_GATE_RISE_FALL)} {_fmt(t_discharge_end - t_discharge_on)} {_fmt(Tsw)})",
        f"Vgate_sync gate_sync 0 DC 0 PULSE(0 5 {_fmt(t_discharge_on)} {_fmt(_GATE_RISE_FALL)} "
        f"{_fmt(_GATE_RISE_FALL)} {_fmt(t_discharge_end - t_discharge_on)} {_fmt(Tsw)})",
        "",
        f"* duty servo trim {charge_trim:.4f} -> commanded D {D_cmd:.4f} "
        f"(design D {sizing.D:.4f})",
        ".op",
        ".end",
    ]
    return "\n".join(lines) + "\n"


_BUILDERS = {
    "buck": _build_buck,
    "boost": _build_boost,
    "buck_boost": _build_buck_boost,
}


def build_netlist(spec: Spec, sizing: SizingResult, selected: SelectedComponents,
                  charge_trim: float = 1.0) -> str:
    """Dispatch to the right topology's netlist builder.

    `charge_trim` scales the commanded charge-phase duty (design D stays
    untouched); the run_spice duty servo uses it to hold the simulated
    operating point at the design Vout/Iout despite static drops (dead-time
    diode clamp, DCR, Ron) — the open-loop equivalent of what the closed
    loop does in hardware."""
    try:
        builder = _BUILDERS[sizing.topology]
    except KeyError:
        raise ValueError(f"No netlist builder for topology {sizing.topology!r}") from None
    return builder(spec, sizing, selected, charge_trim=charge_trim)


def suggested_charge_trim(spec: Spec, sizing: SizingResult, selected: SelectedComponents) -> float:
    """Analytic volt-second-balance estimate of the charge-window trim that
    holds the open-loop rig at the design Vout despite static drops
    (dead-time body-diode clamp, DCR, Ron). The run_spice duty servo refines
    this starting estimate against the actual waveforms.

    Per topology (Tsw = period, td = dead time, 3 dead windows/period, Vf =
    body-diode clamp of the Dbody model):
      buck:   Vin*t_on = (Vout + I*DCR)*Tsw + Vf*3td + I*Ron*(Tsw - 3td)
      boost:  (Vout + I*Ron)*t_sync - Vf*3td = (Vin - I*DCR)*Tsw,
              t_sync = Tsw - 3td - t_ctrl
      buck_boost: charge/discharge each carry i_L through two series switches;
              I_L = Iout/(1-D) (delivered only during discharge).
    """
    Tsw = 1.0 / spec.fsw
    td = _dead_time(selected)
    dead = 3.0 * td
    Ron = selected.mosfet.Rds_on
    DCR = selected.inductor.DCR
    Vf = 0.7  # Dbody clamp level (Is=1e-9, N=1.0 at load current)
    D = min(max(sizing.D, 0.05), 0.95)
    Dp = 1.0 - D

    try:
        if sizing.topology == "buck":
            i_l = spec.Iout
            t_on = ((spec.Vout + i_l * DCR) * Tsw + dead * Vf
                    + i_l * Ron * (Tsw - dead)) / spec.Vin
        elif sizing.topology == "boost":
            i_l = spec.Iout / Dp
            t_on = Tsw - dead - ((spec.Vin - i_l * DCR) * Tsw + dead * Vf) \
                / (spec.Vout + i_l * Ron)
        else:  # buck_boost (4-switch non-inverting)
            i_l = spec.Iout / Dp
            r_series = 2.0 * Ron + DCR
            a_c = spec.Vin - i_l * r_series    # charge-phase volt scale
            a_d = spec.Vout + i_l * r_series   # discharge-phase volt scale
            t_on = (a_d * (Tsw - dead) + dead * (spec.Vout + Vf + i_l * DCR)) / a_c
        # t_on is the desired CONDUCTING width; the builders derive that width
        # as trim*D*Tsw - td (each phase ends td early), so invert that.
        if not (t_on > 0.0 and t_on < Tsw - dead):
            return 1.0
        return float(min(max((t_on + td) / (D * Tsw), 0.3), 3.0))
    except Exception:
        return 1.0


def write_netlist(netlist_text: str, path: str | Path) -> Path:
    """Write the netlist text to disk, creating parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(netlist_text, encoding="utf-8")
    return p


@dataclass
class LintResult:
    ok: bool
    log: str


def lint_netlist(cir_path: str | Path, ngspice_bin: str = "ngspice") -> LintResult:
    """Dry-run the netlist through `ngspice -b` and check for parse errors.

    This runs `.op` (an operating-point solve), not a `.tran` -- it validates
    that every device/model/node reference parses and the circuit is
    solvable, without doing the real transient work that's Phase 4's job.

    Success criterion (audit fix): ngspice's exit code is not a reliable
    success signal by itself (non-zero on some benign cases), and scanning
    for the substring "error" alone false-passes differently-worded
    failures. The gate is now: exit code 0 AND no ngspice error markers
    ("error"/"fatal error"/"unknown device"/"could not" parse failures) in
    the combined log. That is still a heuristic, but it fails closed on the
    known ngspice failure phrasings instead of only on the literal word.
    """
    p = Path(cir_path)
    try:
        proc = subprocess.run(
            [ngspice_bin, "-b", str(p)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as e:
        raise NetlistLintError(f"'{ngspice_bin}' not found on PATH -- is ngspice installed?") from e
    log = proc.stdout + proc.stderr
    failure_markers = ("error", "fatal", "unknown device", "could not", "not found",
                       "no such ", "failed to parse", "syntax")
    lowered = log.lower()
    ok = proc.returncode == 0 and not any(m in lowered for m in failure_markers)
    return LintResult(ok=ok, log=log)


def build_and_write(
    spec: Spec,
    sizing: SizingResult,
    selected: SelectedComponents,
    path: str | Path,
    lint: bool = True,
) -> tuple[Path, LintResult | None]:
    """Build, write, and (by default) lint a netlist in one call."""
    text = build_netlist(spec, sizing, selected)
    out_path = write_netlist(text, path)
    result = lint_netlist(out_path) if lint else None
    if lint and not result.ok:
        raise NetlistLintError(f"Generated netlist failed ngspice lint:\n{result.log}")
    return out_path, result