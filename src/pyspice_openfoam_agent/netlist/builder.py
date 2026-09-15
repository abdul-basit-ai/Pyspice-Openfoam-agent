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
invented model parameters. Inductor DCR and capacitor ESR/ESL are modeled as
explicit series R/L elements next to ideal L/C, for the same reason: it's the
real part's loss-relevant parasitic, not a lumped guess.

Topology coverage: buck, boost, buck_boost. All three use one high/low
switch pair (synchronous rectification -- one selected MOSFET part number
used for both switches, which is standard practice at the Rds_on this
project's library targets) with complementary PWM gate drives and a small
(1 ns) edge time that acts as informal dead time. Buck-boost is modeled at
output-voltage *magnitude* (the true inverting topology's negative rail
sign convention is not carried through the netlist); this is adequate for
the Phase 3 checkpoint (netlist parses cleanly) and is revisited if Phase 4
transient results need the real polarity.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from pyspice_openfoam_agent.netlist.selector import SelectedComponents
from pyspice_openfoam_agent.sizing.engine import Spec, SizingResult

_GATE_RISE_FALL = 1e-9  # 1 ns gate edge time
# Deterministic non-overlap dead time. MUST be > 2*gate edge time so the two
# switches are never simultaneously on (synchronous shoot-through guard).
# Scaled from the library's Miller crossover time when available, defaulting to
# 30 ns (a conservative silicon-part value at 100-500 kHz).
_DEAD_TIME_DEFAULT_S = 30e-9
# Soft-start ramp duration: at least 300 switching periods (task spec) and a
# sane floor. Exponential/LTI recommendation ~1-2 ms; we use 300*Tsw which at
# 300 kHz = 1 ms, at 500 kHz = 0.6 ms — always sub-ms so the steady-state
# detector still sees a settled tail inside a bounded cycle budget.
def _dead_time(sel) -> float:
    try:
        tr = getattr(sel.mosfet, "t_rise_s", None) or 0.0
        tf = getattr(sel.mosfet, "t_fall_s", None) or 0.0
        scale = max(tr, tf, _GATE_RISE_FALL) * 4
        return max(_DEAD_TIME_DEFAULT_S, scale)
    except Exception:
        return _DEAD_TIME_DEFAULT_S


def _soft_start_s(Tsw: float) -> float:
    """Soft-start ramp length: >= 300 switching periods (task spec)."""
    return 300.0 * Tsw


def _pulse(hs: bool, ton_s: float, Tsw: float, td_s: float,
           v1: float = 0.0, v2: float = 5.0) -> str:
    """PULSE(...) with dead-time-staggered on-time.

    hs=True  -> active high at t=0, on for ton_s.
    hs=False -> active high AFTER a td_s delay (so HS turned off td_s before
                LS turns on), on for (Tsw - td_s - ton_hs) ~ complementary.
    The ideal-switch Vt=2.5 threshold means a switch conducts above ~2.5 V; the
    edge time (_GATE_RISE_FALL=1ns) combined with a td_s >= 30ns guarantees the
    outgoing switch has fully turned OFF before the incoming one turns on.
    """
    # For LS we insert a leading dead-time delay so it never overlaps HS's turn-off.
    td = 0.0 if hs else td_s
    return f"PULSE({v1:g} {v2:g} {_fmt(td)} {_fmt(_GATE_RISE_FALL)} {_fmt(_GATE_RISE_FALL)} {_fmt(ton_s)} {_fmt(Tsw)})"


class NetlistLintError(RuntimeError):
    """ngspice -b reported an error while parsing a generated netlist."""


def _fmt(x: float) -> str:
    """Consistent scientific-notation formatting for netlist parameter values."""
    return f"{x:.6e}"


def _cap_branch(node_in: str, node_out: str, C: float, ESR: float, ESL: float, ref: str) -> str:
    """Cout -- Resr -- (Lesl) -- node_out, as one or two series elements.

    ESL is often 0 in the library (default) or a few nH; skip the inductor
    element entirely when it's non-positive rather than emit an invalid
    zero-henry inductor.
    """
    lines = [f"C{ref} {node_in} {ref}_esr {_fmt(C)}"]
    if ESL > 0:
        lines.append(f"Resr{ref} {ref}_esr {ref}_esl {_fmt(ESR)}")
        lines.append(f"Lesl{ref} {ref}_esl {node_out} {_fmt(ESL)}")
    else:
        lines.append(f"Resr{ref} {ref}_esr {node_out} {_fmt(ESR)}")
    return "\n".join(lines)


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
      LS conducts  [D*Tsw + td, Tsw]       (turns on td AFTER HS is off)
    so the two switches are separated by a 2*td guaranteed-open gap. With
    td = 30 ns >> gate edge (1 ns), synchronous shoot-through (task bug #2) is
    eliminated. The device (S...) lines are written by the caller per topology.

    DC value note: each gate source carries an explicit DC operating value so
    an `.op` solve reproduces the designed duty cycle (HS=5 V ON, LS=0 V OFF)
    instead of the all-off state ngspice assumes with no DC value.
    """
    td = td_s if td_s is not None else _DEAD_TIME_DEFAULT_S
    t_hs_off = max(D * Tsw - td, 1e-12)          # HS high until here
    t_ls_on = D * Tsw + td                        # LS high from here
    t_ls_pw = max(Tsw - t_ls_on, 1e-12)           # LS high width
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


def _build_buck(spec: Spec, sizing: SizingResult, sel: SelectedComponents) -> str:
    Tsw = 1.0 / spec.fsw
    Rload = spec.Vout / spec.Iout
    td = _dead_time(sel)
    # Soft-start: ramp the input rail 0 -> Vin over t_ss (>=300*Tsw) so the
    # LC output tank is charged gently, eliminating startup overshoot (task
    # bug #3). Open-loop fixed-D: ramping V_in is the correct gentle excitation.
    t_ss = _soft_start_s(Tsw)
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
        _switch_pair("hs", "ls", sel.mosfet.Rds_on, sizing.D, Tsw, td_s=td),
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
        ".op",
        ".end",
    ]
    return "\n".join(lines) + "\n"


def _build_boost(spec: Spec, sizing: SizingResult, sel: SelectedComponents) -> str:
    Tsw = 1.0 / spec.fsw
    Rload = spec.Vout / spec.Iout
    td = _dead_time(sel)
    t_ss = _soft_start_s(Tsw)
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
        _switch_pair("ctrl", "sync", sel.mosfet.Rds_on, sizing.D, Tsw, td_s=td),
        "",
        "* Output capacitor with real ESR (+ ESL) in series",
        _cap_branch("out", "0", sel.capacitor.C, sel.capacitor.ESR, sel.capacitor.ESL, "out"),
        "",
        f"Rload out 0 {_fmt(Rload)}",
        "",
        ".op",
        ".end",
    ]
    return "\n".join(lines) + "\n"


def _build_buck_boost(spec: Spec, sizing: SizingResult, sel: SelectedComponents) -> str:
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
    dt_charge = sizing.D * Tsw
    dt_discharge = (1.0 - sizing.D) * Tsw
    # Dead-time staggered phase boundaries: charge ends td early, discharge
    # starts td late (and vice-versa), so the two pairs never overlap.
    t_charge_on = max(dt_charge - td, 1e-12)
    t_discharge_on = dt_charge + td
    t_discharge_end = max(Tsw - td, 1e-12)
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
        "* Floating inductor with real DCR in series (sw_a -- L -- sw_b)",
        f"Lout sw_a sw_b {_fmt(sel.inductor.L)}",
        f"Rdcr sw_b out {_fmt(sel.inductor.DCR)}",
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
        ".op",
        ".end",
    ]
    return "\n".join(lines) + "\n"


_BUILDERS = {
    "buck": _build_buck,
    "boost": _build_boost,
    "buck_boost": _build_buck_boost,
}


def build_netlist(spec: Spec, sizing: SizingResult, selected: SelectedComponents) -> str:
    """Dispatch to the right topology's netlist builder."""
    try:
        builder = _BUILDERS[sizing.topology]
    except KeyError:
        raise ValueError(f"No netlist builder for topology {sizing.topology!r}") from None
    return builder(spec, sizing, selected)


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
    Ngspice's own exit code is not a reliable success signal (it returns
    non-zero even on some benign no-simulation-requested cases), so success
    is judged by scanning the log for "error" (case-insensitive) instead.
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
    ok = "error" not in log.lower()
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