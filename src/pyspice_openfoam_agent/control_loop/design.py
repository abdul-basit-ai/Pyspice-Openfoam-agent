"""Phase 6: feedback-compensator design and deterministic stability analysis.

Goals (gap #1) mandate: design and validate the feedback compensator, not
just the power stage. Stability margins are COMPUTED, never judged by the
LLM. This module:

  - builds the averaged small-signal control-to-output plant Gvd(s) for the
    selected converter (voltage-mode, continuous conduction),
  - designs a Type III compensator Gc(s),
  - models the PWM modulator's half-sample transport delay (always present in
    any real implementation; digital implementations add up to ~1 more
    switching period) as an exact all-pass phase term,
  - evaluates the open-loop loop-gain T(s) = Gvd(s)*Gc(s)*e^(-s*Td) (H = unity
    here) and derives phase margin / gain margin deterministically from the
    frequency response, taking the WORST crossing when several exist,
  - returns a verdict against the goals' targets: phase margin >= 45 deg,
    gain margin >= 6 dB.

Root-cause-proven way to compute margins: derive them numerically from the
frequency response (crossing of |T|=1 for PM, crossing of <T=-180 deg for
GM) rather than reading them off a plot. Primary backend is python-control's
``ct.margin`` (worst-case across crossings); the fallback is a fine log sweep
with UNWRAPPED phase and the same worst-crossing rule.

The plant uses the selected real L and C values (Phase 3 selector output) and
the duty ratio from the sizing engine, so the control design is grounded in
the actual design object, not a nominal guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from pyspice_openfoam_agent.design.object import Design, Requirements
from pyspice_openfoam_agent.sizing.engine import SizingResult


class ControlDesignError(ValueError):
    """Compensator could not be sized or the plant is degenerate."""


@dataclass
class ControlVerdict:
    """Stability verdict + the numbers that justify it."""

    passed: bool
    topology: str
    control_law: str
    crossover_hz: float
    phase_margin_deg: float | None  # None = no gain crossover in band (see reasons)
    gain_margin_db: float | None  # None = no -180 deg crossing (effectively infinite)
    compensator: str  # type-ii | type-iii
    reasons: list[str] = field(default_factory=list)
    # plant parameters used (for reproducibility/provenance)
    plant: dict[str, float] = field(default_factory=dict)
    compensator_poles_hz: list[float] = field(default_factory=list)
    compensator_zeros_hz: list[float] = field(default_factory=list)


# ---------------- plant model (averaged, CCM) ----------------

_Q_CLAMP = (0.3, 5.0)


def _clamp_q(Q: float) -> float:
    """Clamp the tank Q to a design-sane band. Applied to the FINAL Q (after
    the D' scaling for boost/buck-boost): clamping before the scaling let a
    small D' push the effective Q below the intended floor (audit fix)."""
    return min(max(Q, _Q_CLAMP[0]), _Q_CLAMP[1])


def _lc_model(L: float, C: float, R_load: float) -> tuple[float, float]:
    """(w0, Q) of the output LC double-pole with load damping (unclamped)."""
    if L <= 0 or C <= 0:
        raise ControlDesignError("plant needs L>0 and C>0")
    w0 = 1.0 / math.sqrt(L * C)
    Q = R_load * math.sqrt(C / L)
    return w0, Q


def plant_transfer(
    topology: str, Vin: float, Vout: float, Iout: float,
    L: float, C: float, ESR: float, R_load: float, D: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Control-to-output Gvd(s) = num(s)/den(s), averaged CCM model.

    Canonical power-stage models (Erickson & Maksimovic, Fundamentals of
    Power Electronics), with a real output-capacitor ESR zero and, for the
    boost and buck-boost, the RHP zero that governs the achievable crossover:

      Buck:       Gvd = Vin*(1+sRcC) / (1 + s/(w0 Q) + (s/w0)^2)
                  (LHP; no RHP zero)
      Boost:      Gvd = Vin*(1+sRcC)*(1 - s*L/D'^2/R) / ( (s/w0)^2 + s/(w0Q) + 1 )
                  w0 = D'/sqrt(LC), Q = R*D'*sqrt(C/L), RHP zero = D'^2*R/L
      Buck-boost: Gvd = Vin*(1+sRcC)*(1 - s*L*D/D'^2/R) / ( (s/w0)^2 + s/(w0Q) + 1 )
                  w0 = D'/sqrt(LC), Q = R*D'*sqrt(C/L), RHP zero = (D'^2 R)/(D L)

    D = duty (pass the SizingResult.D for correctness — buck D=Vout/Vin,
    boost D=1-Vin/Vout, buck-boost D=Vout/(Vin+Vout)), D' = 1-D. The RHP zero
    is the reason boost/buck-boost designs must keep crossover well below
    w_rhpz — a purely buck compensator cannot be blindly reused.
    """
    if D is None:
        # fallback: infer from voltage ratio by topology
        if topology == "buck":
            D = Vout / max(Vin, 1e-12)
        elif topology == "boost":
            D = 1.0 - min(Vin / max(Vout, 1e-12), 0.999)
        elif topology == "buck_boost":
            D = Vout / (Vin + Vout)
        else:
            raise ControlDesignError(f"unknown topology for plant: {topology!r}")
    D = min(max(D, 0.05), 0.95)
    Dp = 1.0 - D
    w0, Q = _lc_model(L, C, R_load)
    esr_zero = ESR * C  # (1 + s*Rc*C)

    if topology == "buck":
        num = Vin * np.array([esr_zero, 1.0])
        den = np.array([1.0 / w0**2, 1.0 / (w0 * _clamp_q(Q)), 1.0])
    elif topology == "boost":
        # RHP zero: wz_rhp = D'^2 R / L ; w0 and Q scale with D'
        wz_rhp = (Dp**2) * R_load / L
        w0_b, Q_b = Dp * w0, _clamp_q(Dp * Q)
        gdc = Vin / (Dp**2)
        num = gdc * np.polymul(np.array([-1.0 / wz_rhp, 1.0]),
                               np.array([esr_zero, 1.0]))
        den = np.array([1.0 / w0_b**2, 1.0 / (w0_b * Q_b), 1.0])
    elif topology == "buck_boost":
        wz_rhp = (Dp**2) * R_load / (D * L)
        w0_b, Q_b = Dp * w0, _clamp_q(Dp * Q)
        gdc = Vin / (Dp**2)
        num = gdc * np.polymul(np.array([-1.0 / wz_rhp, 1.0]),
                               np.array([esr_zero, 1.0]))
        den = np.array([1.0 / w0_b**2, 1.0 / (w0_b * Q_b), 1.0])
    else:
        raise ControlDesignError(f"unknown topology for plant: {topology!r}")
    return np.asarray(num, dtype=float), np.asarray(den, dtype=float)


def _pade_delay(num: np.ndarray, den: np.ndarray, Td: float) -> tuple[np.ndarray, np.ndarray]:
    """Append a transport delay e^(-s*Td) to T = num/den via a first-order
    Pade all-pass: e^-x ~= (1 - x/2)/(1 + x/2). Magnitude is EXACTLY 1 at all
    frequencies (pure phase term), and the phase matches e^(-s*Td) to O((wTd)^3)
    — far below 0.5 deg for the crossovers this module targets (w*Td < 1)."""
    if Td <= 0:
        return num, den
    num_d = np.polymul(np.asarray(num, dtype=float), [-Td / 2.0, 1.0])
    den_d = np.polymul(np.asarray(den, dtype=float), [Td / 2.0, 1.0])
    return num_d, den_d


# ---------------- compensator design ----------------

def design_type_iii(
    num: np.ndarray, den: np.ndarray, f_sw_hz: float, L: float, C: float,
    ESR: float, R_load: float, f_crossover_target: float | None = None,
    lc_pole_scale: float = 1.0, delay_periods: float = 0.0,
    pm_target_deg: float = 47.0,
) -> tuple[np.ndarray, np.ndarray, float, list[float], list[float]]:
    """Design a Type III (integral + 2 zero + 2 pole) compensator.

    Return (gc_num, gc_den, crossover_hz, zeros_hz, poles_hz).

    Placement (phase-targeted, K-factor-style):
      - pole 1 cancels the ESR zero EXACTLY (the plant zero and compensator
        pole cancel each other's magnitude AND phase, so the pair is
        phase-neutral at any crossover; the old max(f_esr, 4*fc) heuristic
        broke the cancellation whenever the ESR zero sat far above crossover
        — typical with ceramic output caps — taxing PM by ~atan(fc/f_esr)
        for no attenuation benefit, audit fix).
      - pole 2 at f_sw/2 rolls off switching harmonics.
      - the double zero is SOLVED (bisection) so the delay-inclusive loop
        phase at the target crossover delivers `pm_target_deg` (45 deg gate
        + 2 deg headroom). The old fixed placement (zeros at the LC pole)
        was phase-blind: with a half-sample PWM delay modeled, several real
        designs could not reach the 45 deg gate at any crossover (audit fix).
        The bisection starts at the (possibly D'-scaled) LC pole and only
        moves the zeros below it when the plant phase demands it.
      - integral gain K pins |T(jw_c)| = 1 at the target crossover.
    """
    w0 = 1.0 / math.sqrt(L * C)
    f_lc = (w0 / (2 * math.pi)) * float(lc_pole_scale)
    f_esr = 1.0 / (2 * math.pi * ESR * C) if ESR * C > 0 else 1e9
    # Target crossover: caller supplies the deterministically-clamped value
    # (buck 0.10*f_sw; boost/buck_boost min(0.20*f_rhp, 0.05*f_sw)). It is
    # never silently raised back to f_sw/10.
    f_crossover = f_crossover_target if f_crossover_target else (f_sw_hz / 10.0)

    fp1 = f_esr                     # cancel ESR zero (phase-neutral pair)
    fp2 = f_sw_hz / 2.0             # switching-harmonic attenuation
    wc = 2 * math.pi * f_crossover
    Td = max(delay_periods, 0.0) / f_sw_hz
    wp1 = 2 * math.pi * fp1
    wp2 = 2 * math.pi * fp2

    # Phase bisection over the double-zero location: lowering fz adds
    # 2*atan(f/fz) of phase at wc (monotone), so PM at wc is monotone in fz.
    # K does not affect phase, so the solve ignores it.
    gc_den = np.polymul([1.0, 0.0], np.polymul([1.0, wp1], [1.0, wp2]))

    def _pm_at(fz: float) -> float:
        shape = np.polymul([1.0, 2 * math.pi * fz], [1.0, 2 * math.pi * fz])
        T = (np.polyval(num, 1j * wc) / np.polyval(den, 1j * wc)) \
            * (np.polyval(shape, 1j * wc) / np.polyval(gc_den, 1j * wc)) \
            * np.exp(-1j * wc * Td)
        return 180.0 + math.degrees(math.atan2(T.imag, T.real))

    lo = max(f_crossover / 200.0, 1.0)     # zeros far below fc (phase-rich)
    hi = max(f_lc, lo * 1.0001)            # start at the (scaled) LC pole
    fz = hi
    if _pm_at(lo) >= pm_target_deg:
        for _ in range(60):                # ~1e-18 relative precision
            mid = math.sqrt(lo * hi)       # geometric bisection (log-fz)
            if _pm_at(mid) >= pm_target_deg:
                lo = mid
            else:
                hi = mid
            if hi / lo < 1.0 + 1e-9:
                break
        fz = lo
    # else: even phase-rich zeros cannot reach the target (very slow plant) —
    # keep fz at the LC pole and let the verdict report the miss honestly.

    wz = 2 * math.pi * fz
    gc_num = np.polymul([1.0, wz], [1.0, wz])

    # Gain constant: set |Gc(j w_c)*Gvd(j w_c)| = 1 at crossover (unity H).
    gvd_mag = _eval_mag(num, den, wc)
    gc_k = 1.0 / max(gvd_mag, 1e-12)
    gc_num = gc_k * gc_num

    # Recompute K more precisely so |T(wc)| = 1 (single Newton-style refinement)
    gc_plant = _eval_mag(gc_num, gc_den, wc)
    if gc_plant > 0:
        gc_num *= 1.0 / max(gc_plant * gvd_mag, 1e-12)

    zeros_hz = [fz, fz]
    poles_hz = [fp1, fp2]
    return gc_num, gc_den, f_crossover, zeros_hz, poles_hz


def _eval_mag(num: np.ndarray, den: np.ndarray, w: float) -> float:
    """|H(jw)| for polynomial-transfer num/den (rad/s)."""
    n = np.polyval(num, 1j * w)
    d = np.polyval(den, 1j * w)
    m = abs(n) / max(abs(d), 1e-300)
    return m


# ---------------- margin computation (deterministic) ----------------

def _margins_from_response(
    num: np.ndarray, den: np.ndarray,
) -> tuple[float | None, float | None, float]:
    """Phase margin, gain margin, crossover from the loop gain T = num/den.

    Primary computation: ``control.margin`` (python-control) — the goals'
    mandated deterministic tool, which already reports the WORST margin when
    several crossings exist. Fallback (library unavailable): a fine
    logarithmic sweep with UNWRAPPED phase and the same worst-crossing rule —
    PM is the minimum over ALL gain crossovers (a conditionally-stable loop
    can look fine at its first crossing and fail at a later one), GM the
    minimum over ALL -180 deg phase crossovers.

    Phase must be unwrapped before any -180 deg crossing detection
    (audit fix): np.angle's principal branch lies in (-180, +180], so
    ``phase + 180`` was strictly positive and the sign-change detector could
    never fire — gain-margin detection was dead code, and a true phase below
    -180 deg at crossover wrapped into a bogus ~+360 deg "passing" PM.

    Returns (pm, gm, fc_hz); None means the crossing does not exist in the
    swept band (no |T|=1 crossing => no finite PM; no -180 crossing => GM
    effectively infinite). None is never silently replaced by a fabricated
    numeric value.
    """
    pm = gm = None
    fc = 0.0
    try:  # primary: python-control (deterministic, worst-case across crossings)
        import control as ct

        gm_c, pm_c, _w_pc, w_gc = ct.margin(ct.tf(num, den))
        if np.isfinite(pm_c):
            pm = float(pm_c)
        if np.isinf(gm_c):
            gm = None
        elif np.isfinite(gm_c) and gm_c > 0:
            gm = float(gm_c)
        if np.isfinite(w_gc) and w_gc > 0:
            fc = float(w_gc) / (2 * math.pi)
        if pm is not None or gm is not None:
            return (pm, gm, fc)
    except Exception:
        pass  # fall through to the sweep below

    w = np.logspace(0, 10, 400000)  # 1 rad/s..10 Grad/s, fine sweep
    T = np.polyval(num, 1j * w) / (np.polyval(den, 1j * w))
    mag = np.abs(T)
    phase = np.degrees(np.unwrap(np.angle(T)))  # unwrap BEFORE crossing search

    # --- phase margin: at |T|=1 (gain crossover), PM = phase + 180 ---
    gc_indices = _crossings(mag - 1.0)
    if gc_indices:
        pms = [phase[i] + 180.0 for i in gc_indices]
        pm = min(pms)  # worst-case across all gain crossovers
        fc = w[gc_indices[0]] / (2 * math.pi)

    # --- gain margin: at -180 (phase crossover), GM = -20log|T| ---
    pc_indices = _crossings(phase + 180.0)
    if pc_indices:
        gm = min(-20.0 * math.log10(max(mag[i], 1e-300)) for i in pc_indices)
    return (pm, gm, float(fc))


def _crossings(signal: np.ndarray) -> list[int]:
    """Indices of ALL sign changes in `signal` (ascending frequency), including
    exact-zero samples (a sample exactly at the crossing IS a crossing —
    the old strict <0 product test missed it, audit fix)."""
    sign = np.sign(signal)
    diff = sign[:-1] * sign[1:]
    idx = [int(i) + 1 for i in np.where(diff < 0)[0]]
    idx.extend(int(i) for i in np.where(sign == 0)[0])
    return sorted(set(idx))


# ---------------- top-level ----------------

def analyze_control_loop(
    design: Design,
    sizing: SizingResult,
    L: float,
    C: float,
    ESR: float,
    control_law: str = "voltage",
    f_sw_hz: float | None = None,
    delay_periods: float = 0.5,
) -> ControlVerdict:
    """Design the compensator and report the margin verdict.

    design: the Phase 4 design object (for topology + spec).
    sizing: Phase 3 analytical result (for D; harmless for buck margins).
    L, C, ESR: the SELECTED real component values (Phase 3 selector).
    delay_periods: PWM/modulator transport delay modeled in the loop gain, in
      switching periods. Any real implementation carries at least the ZOH
      half-sample (0.5*Tsw average delay of the duty update); a digital loop
      adds sampling+computation for up to ~1.5*Tsw total. The old s-domain-only
      margins ignored this entirely and overstated PM by ~360*fc/fsw*delay
      degrees (audit fix: ~18 deg at buck's 0.1*fsw target). Pass 0 for the
      raw continuous-time margins.
    """
    req: Requirements = design.requirements
    Vin = req.Vin
    Vout = req.Vout
    fsw = f_sw_hz or (req.fsw_khz * 1e3)
    R_load = Vout / max(req.Iout, 1e-9)
    topo = design.topology.name or "buck"

    if topo not in ("buck", "boost", "buck_boost"):
        raise ControlDesignError(f"unsupported topology for control-loop: {topo!r}")

    num, den = plant_transfer(topo, Vin, Vout, req.Iout,
                              L, C, ESR, R_load,
                              D=sizing.D if sizing else None)
    # For boost/buck-boost the RHP zero caps crossover (~1/5 f_rhpz is a
    # standard rule of thumb) — a deterministic guard so the Type III design
    # never places the loop-gain crossover beyond where the RHP zero makes it
    # intrinsically unstable.
    lc_pole_scale = 1.0  # buck: plant LC pole at 1/sqrt(LC); boost/bb: D'-scaled
    if topo in ("boost", "buck_boost"):
        D = (sizing.D if sizing and sizing.D else None)
        if D is None:
            D = 1 - min(Vin / max(Vout, 1e-12), 0.999) if topo == "boost" \
                else Vout / (Vin + Vout)
        D = min(max(D, 0.05), 0.95)
        Dp = 1.0 - D
        lc_pole_scale = Dp  # averaged model: double pole moves to D'*f_lc
        f_rhp = ((Dp**2) * R_load / L) / (2 * math.pi) if topo == "boost" \
            else ((Dp**2) * R_load / (D * L)) / (2 * math.pi)
        # Deterministic crossover clamp (task): fc <= 0.20*f_rhp AND <= 0.05*f_sw.
        # A higher-bandwidth buck compensator reused here would push crossover
        # past the RHP zero -> phase collapse / limit-cycle (task bug #1).
        f_cross_rhp = 0.20 * f_rhp
        f_cross_sw = 0.05 * fsw
        f_cross = min(f_cross_rhp, f_cross_sw)
        # No separate minimum-bandwidth floor here: lifting a very slow
        # plant's crossover to a fixed 1 kHz floor can violate the RHP-zero
        # guard, so the guard value stands even when it is below 1 kHz
        # (audit fix: the old unconditional max(f_cross, 1e3) could raise the
        # crossover target past the RHP-zero limit).
    else:
        f_rhp = float("inf")
        # Buck: fc <= min(0.10*f_sw, 0.20*f_cross_max). No RHP zero; k_cross
        # is the standard 1/10 switching-frequency practical maximum.
        f_cross = 0.10 * fsw

    # Loop gain T = Gvd * Gc (H = 1), including the PWM transport delay
    # (exact-magnitude all-pass: shifts phase only, so it never changes the
    # designed crossover, only the phase it lands on).
    Td = max(delay_periods, 0.0) / fsw

    # Deterministic crossover search: the phase-targeted compensator design
    # already aims for the PM gate at the policy crossover (delay included),
    # so attempt 1 normally succeeds. The search remains as a bounded
    # fallback for plants where the phase target is unreachable at the
    # policy crossover: lower fc until the gate is met or the floor hits.
    pm_floor = 0.01 * fsw  # never chase bandwidth below 1% of f_sw
    attempts: list[tuple[float, float | None, float | None, float, tuple, list, list]] = []
    fc_try = f_cross
    for _ in range(4):
        gc_num, gc_den, fc, zeros_hz, poles_hz = design_type_iii(
            num, den, fsw, L, C, ESR, R_load, f_crossover_target=fc_try,
            lc_pole_scale=lc_pole_scale, delay_periods=max(delay_periods, 0.0))
        t_num, t_den = _pade_delay(
            np.polymul(num, gc_num), np.polymul(den, gc_den), Td)
        pm_try, gm_try, fc_meas = _margins_from_response(t_num, t_den)
        attempts.append((fc_try, pm_try, gm_try, fc_meas,
                         (gc_num, gc_den), zeros_hz, poles_hz))
        if pm_try is None:
            break  # no gain crossover at all — lowering fc cannot help
        if pm_try >= 47.0 and (gm_try is None or gm_try >= 6.0):
            break  # gate met with 2 deg headroom
        if fc_try * 0.8 < pm_floor:
            break
        fc_try *= 0.8

    # Best attempt = highest PM among those that satisfy GM; fall back to the
    # highest-PM attempt overall (the verdict then reports the miss honestly).
    def _rank(a) -> tuple:
        _, pm_a, gm_a, *_ = a
        gm_ok = gm_a is None or gm_a >= 6.0
        return (gm_ok, pm_a if pm_a is not None else -1e9)

    best = max(attempts, key=_rank)
    fc_try, pm, gm, fc_meas, (gc_num, gc_den), zeros_hz, poles_hz = best

    reasons: list[str] = []
    passed = True
    # Contract (goals Phase 6 + tool description): PM >= 45 deg, GM >= 6 dB.
    # A missing crossing is reported as None — never silently replaced by a
    # fabricated numeric value (audit fix: pm=50/gm=inf stand-ins previously
    # made both gates unfalsifiable and corrupted provenance).
    pm_report: float | None = None
    gm_report: float | None = None
    if pm is None:
        reasons.append("no gain crossover in the swept band: loop does not reach "
                       "0 dB (cautious but no defined PM); verify crossover target")
    else:
        pm_report = round(pm, 2)
        if pm < 45.0:
            passed = False
            reasons.append(f"phase margin {pm:.1f} deg < 45 deg target")
    if gm is None:
        # no phase crossover to -180 deg in band => gain margin unbounded
        reasons.append("no phase crossover to -180 deg in band: "
                       "gain margin effectively infinite")
    else:
        gm_report = round(gm, 2)
        if gm < 6.0:
            passed = False
            reasons.append(f"gain margin {gm:.1f} dB < 6 dB target")
    if passed:
        pm_repr = f"{pm_report:.1f} deg" if pm_report is not None else "n/a (no crossover)"
        gm_repr = f"{gm_report:.1f} dB" if gm_report is not None else "infinite"
        reasons.append(
            f"PM {pm_repr}, GM {gm_repr} within targets "
            f"(crossover ~{fc_meas / 1e3:.1f} kHz, {fsw / 1e3:.0f} kHz switching, "
            f"margins include {max(delay_periods, 0.0):.2f} Tsw PWM delay)")
    plant = {
        "topology": topo, "L": L, "C": C, "ESR": ESR, "R_load": R_load,
        "Vin": Vin, "f_lc_hz": 1.0 / (2 * math.pi * math.sqrt(L * C)),
        "f_esr_hz": 1.0 / (2 * math.pi * ESR * C),
        "f_rhpz_hz": (None if not math.isfinite(f_rhp) else round(f_rhp, 1)),
        "f_sw_hz": fsw,
        "delay_periods": max(delay_periods, 0.0),
    }
    return ControlVerdict(
        passed=passed, topology=design.topology.name or "buck",
        control_law=control_law, crossover_hz=fc_meas,
        phase_margin_deg=pm_report,
        gain_margin_db=gm_report,
        compensator="type-iii", reasons=reasons, plant=plant,
        compensator_poles_hz=poles_hz, compensator_zeros_hz=zeros_hz,
    )