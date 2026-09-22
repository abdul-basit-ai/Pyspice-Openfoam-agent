"""Step 3 of the validation plan: inject the EVM's EXACT parts and run the
real pipeline from build_netlist onward — select_components is BYPASSED
(this plan validates the physics engine, not selection logic).

Thin wrapper over the orchestrator tools: builds the schema objects from
validation/evms/<evm_id>/library_entries.yaml, wires them onto a
ToolContext, then dispatches build_netlist -> run_spice ->
electro_thermal_converge. No tool logic is reimplemented here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

from pyspice_openfoam_agent.library.schema import (  # noqa: E402
    Capacitor,
    DatasheetRef,
    Inductor,
    MOSFET,
)
from pyspice_openfoam_agent.netlist.selector import SelectedComponents  # noqa: E402
from pyspice_openfoam_agent.orchestrator.tools import (  # noqa: E402
    ToolContext,
    dispatch,
)
from pyspice_openfoam_agent.sizing.engine import Spec, size  # noqa: E402


def load_evm_entries(evm_id: str) -> dict:
    p = REPO / "validation" / "evms" / evm_id / "library_entries.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _ref(pn: str) -> DatasheetRef:
    return DatasheetRef(url=f"validation://{pn}", page=None,
                        note="EVM validation entry — see library_entries.yaml")


def _mosfet(e: dict) -> MOSFET:
    f = e["status_fields"]
    return MOSFET(
        part_number=e["part_number"], manufacturer=e.get("manufacturer"),
        Vds_max=f["Vds_max"]["value"], Rds_on=f["Rds_on"]["value"],
        Qg=f["Qg"]["value"], Qgd=f["Qgd"]["value"],
        V_plateau=f["V_plateau"]["value"], Ciss=f["Ciss"]["value"],
        Coss=f["Coss"]["value"], Crss=f["Crss"]["value"], Qrr=f["Qrr"]["value"],
        V_F=f["V_F"]["value"], Id_max=f["Id_max"]["value"],
        package=e.get("package", "validation"),
        die_x_mm=f["die_x_mm"]["value"], die_y_mm=f["die_y_mm"]["value"],
        die_z_mm=f["die_z_mm"]["value"], R_theta_jc=f["R_theta_jc"]["value"],
        R_theta_ja=f["R_theta_ja"]["value"],
        datasheet=_ref(e["part_number"]),
    )


def build_fixed_context(evm_id: str, run_dir: Path, iout: float) -> ToolContext:
    e = load_evm_entries(evm_id)
    tc = yaml.safe_load((REPO / "validation" / "evms" / evm_id
                         / "test_conditions.yaml").read_text(encoding="utf-8"))

    hs = _mosfet(e["mosfets"][0])
    ls = _mosfet(e["mosfets"][1])
    li_f = e["inductor"]["status_fields"]
    inductor = Inductor(
        part_number=e["inductor"]["part_number"],
        manufacturer=e["inductor"].get("manufacturer"), L=li_f["L"]["value"],
        DCR=li_f["DCR"]["value"], Isat=li_f["Isat"]["value"],
        Irms=li_f["Irms"]["value"], f_self_res=1e6,
        package="validation", datasheet=_ref(e["inductor"]["part_number"]),
    )
    cb_f = e["output_capacitor_bank"]["status_fields"]
    cap = Capacitor(
        part_number=e["output_capacitor_bank"]["part_number"],
        C=cb_f["C"]["value"], ESR=cb_f["ESR"]["value"],
        V_rated=cb_f["V_rated"]["value"], Irms_max=cb_f["Irms_max"]["value"],
        package="validation", datasheet=_ref("C1210C107M9PACTU"),
    )

    spec = Spec(Vin=tc["vin_v"], Vout=tc["vout_v"], Iout=iout,
                fsw=tc["fsw_hz"], Vripple=0.015, topology_constraint="buck")
    ctx = ToolContext(run_dir=run_dir,
                      library=type("L", (), {"mosfets": {}, "inductors": {},
                                             "capacitors": {}})())
    # sizing feeds the builder its nominal duty; the servo re-trims it anyway
    sizing = size(spec)
    ctx.spec, ctx.sizing = spec, sizing
    ctx.selected = SelectedComponents(mosfet=hs, inductor=inductor,
                                      capacitor=cap,
                                      notes=["EVM-exact injection (plan step 3): "
                                             "select_components bypassed"])
    return ctx


def run_point(evm_id: str, iout: float, run_dir: Path) -> dict:
    """Run the real pipeline at one load point; returns the sim results."""
    ctx = build_fixed_context(evm_id, run_dir, iout)
    out: dict = {"load_a": iout}
    for tool in ("build_netlist", "run_spice", "electro_thermal_converge"):
        r = dispatch(ctx, tool, {})
        out[tool] = r.payload
        if not r.ok:
            out["error"] = f"{tool}: {r.payload.get('error', '?')}"
            break
    else:
        sp = out["run_spice"]
        out.update(sim_efficiency_pct=round(sp.get("efficiency", 0) * 100, 2),
                   sim_ripple_mv=sp.get("ripple_mV"),
                   sim_total_loss_w=sp.get("total_loss_W"))
    return out


if __name__ == "__main__":
    import json
    import tempfile

    evm = sys.argv[1] if len(sys.argv) > 1 else "lm27402_evm"
    iout = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    rd = Path(tempfile.mkdtemp(prefix=f"val_{evm}_"))
    res = run_point(evm, iout, rd)
    print(json.dumps(res, indent=2, default=str)[:1500])
