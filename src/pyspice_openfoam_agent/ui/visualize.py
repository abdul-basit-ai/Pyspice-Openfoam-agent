"""Phase 15: circuit visualization — schemdraw schematics + SPICE waveforms.

Schematic rendering per topology (buck / boost / buck_boost) using the
SELECTED real parts (labels carry part numbers and the values that matter),
plus matplotlib waveform plots from the Phase 4 transient result.

schemdraw draws schematics as matplotlib figures — headless-safe (Agg
backend), no GL needed. These are shown in the UI's Circuit tab and saved
to the run dir for the manifest.
"""

from __future__ import annotations

from pathlib import Path

from pyspice_openfoam_agent.spice.runner import TransientResult


class RenderError(RuntimeError):
    """Visualization failed."""


def _use_agg() -> None:
    import matplotlib

    matplotlib.use("Agg")


def draw_schematic(
    topology: str,
    mosfet_pn: str,
    inductor_pn: str,
    inductor_uh: float,
    capacitor_pn: str,
    capacitor_uf: float,
    out_png: str | Path,
    vin: float,
    vout: float,
) -> Path:
    """Render the converter schematic with the real selected parts labeled.

    Audit fix (VIZ-1): buck LS switch now correctly drawn from the switch
    node (sw), not the output node — the LS shorts sw to ground during the
    off-time, it doesn't sit at the output.
    """
    _use_agg()
    try:
        import schemdraw
        import schemdraw.elements as elm
    except ImportError as e:
        raise RenderError(f"schemdraw not installed: {e}") from e

    d = schemdraw.Drawing()
    d.config(unit=2.5, fontsize=12)

    if topology == "buck":
        # Vin -> HS switch -> sw node -> L -> out
        # LS switch: sw -> gnd (NOT out -> gnd; audit VIZ-1 fix)
        # Cout: out -> gnd; Rload: out -> gnd
        d += elm.SourceV().up().label(f"Vin\n{vin}V")
        d += elm.Line().up(d.unit / 2)
        d += elm.Switch().right(d.unit * 1.5).label(f"HS\n{mosfet_pn}")
        d += elm.Dot()
        sw = d.here
        d += elm.Inductor2().right(d.unit * 1.5).label(f"{inductor_pn}\n{inductor_uh:.1f}µH")
        d += elm.Dot()
        out_node = d.here
        # LS from sw node DOWN to ground (correct buck topology)
        d += elm.Switch().down(d.unit * 1.5).at(sw).label(f"LS\n{mosfet_pn}")
        d += elm.Ground()
        d += elm.Capacitor().down(d.unit * 1.2).at(out_node).label(f"{capacitor_pn}\n{capacitor_uf:.1f}µF")
        d += elm.Ground()
        d += elm.Line().right(d.unit).at(out_node)
        d += elm.Resistor().down(d.unit * 1.2).label(f"Load\n{vout}V")
        d += elm.Ground()
    elif topology == "boost":
        # Vin -> L -> sw node; SW: sw->gnd; Sync: sw->out; Cout: out->gnd; Rload
        d += elm.SourceV().up().label(f"Vin\n{vin}V")
        d += elm.Line().up(d.unit / 2)
        d += elm.Inductor2().right(d.unit * 1.5).label(f"{inductor_pn}\n{inductor_uh:.1f}µH")
        d += elm.Dot()
        sw = d.here
        d += elm.Switch().down(d.unit * 1.5).label(f"SW\n{mosfet_pn}")
        d += elm.Ground()
        d += elm.Line().right(d.unit * 1.2).at(sw)
        d += elm.Switch().right(d.unit * 1.2).label(f"Sync\n{mosfet_pn}")
        d += elm.Dot()
        out_node = d.here
        d += elm.Capacitor().down(d.unit * 1.2).label(f"{capacitor_pn}\n{capacitor_uf:.1f}µF")
        d += elm.Ground()
        d += elm.Line().right(d.unit).at(out_node)
        d += elm.Resistor().down(d.unit * 1.2).label(f"Load\n{vout}V")
        d += elm.Ground()
    else:  # buck_boost (4-switch non-inverting)
        d += elm.SourceV().up().label(f"Vin\n{vin}V")
        d += elm.Line().up(d.unit / 2)
        d += elm.Switch().right(d.unit).label(f"HS\n{mosfet_pn}")
        d += elm.Dot()
        sw_a = d.here
        d += elm.Inductor2().right(d.unit * 1.4).label(f"{inductor_pn}\n{inductor_uh:.1f}µH")
        d += elm.Dot()
        sw_b = d.here
        d += elm.Switch().right(d.unit).label(f"Sync\n{mosfet_pn}")
        d += elm.Dot()
        out_node = d.here
        d += elm.Switch().down(d.unit * 1.4).at(sw_a).label(f"LS_A\n{mosfet_pn}")
        d += elm.Ground()
        d += elm.Switch().down(d.unit * 1.4).at(sw_b).label(f"LS_B\n{mosfet_pn}")
        d += elm.Ground()
        d += elm.Capacitor().down(d.unit * 1.2).at(out_node).label(f"{capacitor_pn}\n{capacitor_uf:.1f}µF")
        d += elm.Ground()
        d += elm.Line().right(d.unit).at(out_node)
        d += elm.Resistor().down(d.unit * 1.2).label(f"Load\n{vout}V")
        d += elm.Ground()

    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(out), dpi=150)
    return out


def plot_waveforms(
    result: TransientResult,
    out_png: str | Path,
    vout_target: float,
) -> Path:
    """Plot the output-voltage waveform (full run + steady-state zoom)."""
    _use_agg()
    import matplotlib.pyplot as plt

    t = result.time
    v = result["out"]
    fig, axes = plt.subplots(2, 1, figsize=(9, 6))

    axes[0].plot(t * 1e3, v, lw=0.8)
    axes[0].axhline(vout_target, color="r", ls="--", lw=1, label=f"target {vout_target}V")
    axes[0].set_xlabel("time (ms)")
    axes[0].set_ylabel("Vout (V)")
    axes[0].set_title("Startup transient (full run)")
    axes[0].legend()

    # zoom on the steady-state tail (last 5%)
    t_tail = t[-1] - (t[-1] - t[0]) * 0.05
    mask = t >= t_tail
    axes[1].plot(t[mask] * 1e6, v[mask], lw=1)
    axes[1].axhline(vout_target, color="r", ls="--", lw=1)
    axes[1].set_xlabel("time (µs)")
    axes[1].set_ylabel("Vout (V)")
    axes[1].set_title("Steady-state zoom (last 5%)")
    ripple = v[mask].max() - v[mask].min()
    axes[1].text(0.02, 0.95, f"ripple ≈ {ripple * 1e3:.1f} mV",
                 transform=axes[1].transAxes, va="top")

    fig.tight_layout()
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out