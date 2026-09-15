"""End-to-end acceptance smoke: one representative spec per supported topology
(buck, boost, buck_boost) driven through EVERY built stage, using the real
orchestrator tool layer (ToolContext + dispatch) — the exact path the agent
drives, no mocks:

  NL spec parse -> memory lookup -> sizing -> fast screening -> component
  selection -> netlist build + lint + connectivity validation -> SPICE
  steady state + health -> loss extraction -> control-loop margins ->
  electro-thermal convergence -> (optional) full CHT thermal solve

Usage (inside the docker-agent container; needs the OpenFOAM entrypoint):

    python3 scripts/topology_smoke.py             # all topologies, no CHT
    python3 scripts/topology_smoke.py --thermal   # include full CHT per topology
    python3 scripts/topology_smoke.py --only buck

Exit code 0 iff every stage of every requested topology passed. A per-run
summary JSON is written under runs/topology_smoke/.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.netlist.validate import validate_netlist
from pyspice_openfoam_agent.orchestrator.tools import ToolContext, dispatch
from pyspice_openfoam_agent.sizing.screening import screen
from pyspice_openfoam_agent.sizing.spec_parser import parse_spec

# One representative spec per topology. buck_boost forces the 4-switch
# non-inverting builder via topology_constraint (ratio alone would classify
# this pair as buck).
CASES = {
    "buck": {
        "text": "Input 12 V, output 5 V at 3 A, switching at 500 kHz, ripple less than 50 mV",
        "expect": {"Vin": 12.0, "Vout": 5.0, "Iout": 3.0, "topology": "buck"},
    },
    "boost": {
        "text": "5 V input to 12 V output, 2 A, 500 kHz switching, ripple under 100 mV",
        "expect": {"Vin": 5.0, "Vout": 12.0, "Iout": 2.0, "topology": "boost"},
    },
    "buck_boost": {
        # 500 kHz (not 300): at 300 kHz L_min ~= 48 uH and NO inductor in the
        # library satisfies the L/Isat/Irms margins simultaneously (the
        # original case was unsatisfiable by construction). At 500 kHz
        # L_min ~= 29 uH and the 47 uH part passes all margins.
        "text": "18 V input, 12 V output at 2 A, switching 500 kHz, ripple under 100 mV",
        "expect": {"Vin": 18.0, "Vout": 12.0, "Iout": 2.0, "topology": "buck_boost"},
        "force_topology": "buck_boost",
    },
}


class Report:
    """Collects per-stage PASS/FAIL lines + structured results for one topology."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lines: list[str] = []
        self.results: dict = {}
        self.failures = 0

    def stage(self, ok: bool, label: str, detail: str = "") -> bool:
        tag = "PASS" if ok else "FAIL"
        self.lines.append(f"[{tag}] {label:<22} {detail}")
        if not ok:
            self.failures += 1
        return ok

    def crash(self, label: str, exc: Exception) -> None:
        self.lines.append(f"[FAIL] {label:<22} crashed: {exc!r}")
        self.lines.extend("    " + ln for ln in traceback.format_exc().splitlines()[-4:])
        self.failures += 1

    def info(self, label: str, detail: str = "") -> None:
        self.lines.append(f"[INFO] {label:<22} {detail}")


def run_topology(name: str, case: dict, do_thermal: bool, root: Path) -> Report:
    rep = Report(name)
    rep.info("spec", case["text"])
    run_dir = root / name
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- stage 1: NL spec parse (goals Phase 2) ---
    try:
        parsed = parse_spec(case["text"])
        req = parsed.requirements
        exp = case["expect"]
        ok = (
            req.Vin == exp["Vin"] and req.Vout == exp["Vout"] and req.Iout == exp["Iout"]
        )
        rep.stage(ok, "spec_parser",
                  f"Vin={req.Vin} Vout={req.Vout} Iout={req.Iout} fsw={req.fsw_khz:.0f}kHz "
                  f"ask-material={len(parsed.missing_material)} ask-safety={len(parsed.missing_safety)}")
        if parsed.contradictions:
            rep.info("parser-contradictions", "; ".join(parsed.contradictions))
        spec_kwargs = req.as_spec_kwargs()
    except Exception as e:  # noqa: BLE001 — report, don't die: later stages still informative
        rep.crash("spec_parser", e)
        return rep

    if case.get("force_topology"):
        spec_kwargs["topology_constraint"] = case["force_topology"]

    ctx = ToolContext(run_dir=run_dir, library=load_library())

    def call(label: str, tool: str, args: dict | None = None) -> dict | None:
        res = dispatch(ctx, tool, args or {})
        payload = res.payload
        rep.results[label] = payload
        if not res.ok:
            rep.stage(False, label, f"tool error: {payload.get('error')}")
            return None
        return payload

    # --- stage 2: long-term memory lookup (Phase 11a read path) ---
    mem = call("memory", "read_design_memory",
               {"Vin": exp["Vin"], "Vout": exp["Vout"], "Iout": exp["Iout"],
                "fsw_khz": req.fsw_khz})
    if mem is not None:
        rep.stage(True, "read_design_memory", f"hits={len(mem.get('hits', []))}")

    # --- stage 3: sizing (Phase 3) ---
    # The size_converter TOOL accepts an explicit topology constraint, so the
    # tool path drives ALL cases (the old script bypassed the tool with a
    # direct engine call over a comment claiming the tool had no topology
    # parameter — tools.py has accepted `topology` since the audit gap was
    # closed).
    sized = call("sizing", "size_converter", {
        "Vin": req.Vin, "Vout": req.Vout, "Iout": req.Iout,
        "fsw_khz": req.fsw_khz, "Vripple": req.ripple_v,
        **({"topology": case["force_topology"]} if case.get("force_topology") else {}),
    })
    if sized:
        ok = sized["topology"] == case["expect"]["topology"]
        rep.stage(ok, "size_converter",
                  f"topo={sized['topology']} D={sized['duty_cycle']:.3f} "
                  f"L_min={sized['L_min_uH']:.2f}uH C_min={sized['C_min_uF']:.2f}uF "
                  f"I_pk={sized['I_peak_A']:.2f}A")

    # --- stage 4: component selection (Phase 5) ---
    sel = call("selection", "select_components")
    if sel:
        rep.stage(True, "select_components",
                  f"{sel['mosfet']} ({sel['Rds_on_mOhm']:.1f}mOhm) + {sel['inductor']} + {sel['capacitor']}")

    # --- stage 5: fast screening on the selected parts (Phase 7, direct call) ---
    if ctx.sizing is not None and ctx.selected is not None:
        try:
            verdict = screen(req.Vin, req.Vout, req.Iout, req.fsw_khz * 1e3, ctx.sizing,
                             ctx.selected.mosfet, ctx.selected.inductor, ctx.selected.capacitor)
            rep.stage(not verdict.rejected, "fast_screening",
                      f"rejected={verdict.rejected} warnings={len(verdict.warnings)}"
                      + ("; " + "; ".join(verdict.warnings) if verdict.warnings else ""))
        except Exception as e:
            rep.crash("fast_screening", e)

    # --- stage 6: netlist build + lint + connectivity (Phase 5) ---
    net = call("netlist", "build_netlist")
    if net:
        try:
            conn = validate_netlist(net["netlist"])
            rep.stage(conn.valid, "validate_connectivity",
                      f"valid={conn.valid} floating={conn.floating_nodes}")
        except Exception as e:
            rep.crash("validate_connectivity", e)
        rep.stage(bool(net.get("lint_ok")), "ngspice_lint", f"lint_ok={net.get('lint_ok')}")

    # --- stage 7: SPICE steady state + losses (Phases 8-9) ---
    spice = call("spice", "run_spice")
    if spice:
        health = spice.get("health", {})
        ok = bool(spice.get("converged")) and bool(health.get("ok"))
        rep.stage(ok, "run_spice",
                  f"conv={spice.get('converged')} cycles={spice.get('cycles_to_steady')} "
                  f"ripple={spice.get('ripple_mV')}mV eff={spice.get('efficiency')} "
                  f"Ploss={spice.get('total_loss_W')}W health_ok={health.get('ok')}")
        if not health.get("ok"):
            rep.info("health-reasons", "; ".join(health.get("reasons", [])))

    # --- stage 8: control loop (Phase 6) ---
    ctl = call("control", "analyze_control_loop")
    if ctl:
        rep.stage(bool(ctl.get("passed")), "control_loop",
                  f"PM={ctl.get('phase_margin_deg')}deg GM={ctl.get('gain_margin_db')}dB "
                  f"fc={ctl.get('crossover_kHz')}kHz")

    # --- stage 9: electro-thermal convergence (Phase 13 loop) ---
    et = call("electro_thermal", "electro_thermal_converge")
    if et:
        rep.stage(bool(et.get("converged")), "electro_thermal",
                  f"Tj={et.get('final_tj_C')}C iters={et.get('iterations')}")

    # --- stage 10: full CHT (Phase 12; opt-in, ~1-2 min each) ---
    if do_thermal:
        th = call("thermal", "run_thermal", {"v_in_m_s": 1.0})
        if th:
            tj = th.get("tj_per_device_C", {})
            rep.stage(bool(th.get("converged")), "cht_solve",
                      f"conv={th.get('converged')} Tj_max={th.get('tj_max_C')}C "
                      f"relax={th.get('relaxation_level')} per-dev={tj}")

    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thermal", action="store_true", help="include full CHT solve per topology")
    ap.add_argument("--only", choices=sorted(CASES), help="run a single topology")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = REPO / "runs" / "topology_smoke" / stamp
    root.mkdir(parents=True, exist_ok=True)

    names = [args.only] if args.only else sorted(CASES)
    reports = [run_topology(n, CASES[n], args.thermal, root) for n in names]

    print()
    total_fail = 0
    for rep in reports:
        print(f"=== {rep.name.upper()} " + "=" * (60 - len(rep.name)))
        for ln in rep.lines:
            print(ln)
        total_fail += rep.failures
        print()

    summary = {
        "stamp": stamp,
        "thermal": args.thermal,
        "topologies": {r.name: {"failures": r.failures, "results": r.results} for r in reports},
        "all_passed": total_fail == 0,
    }
    out = root / "summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"{'ALL PASSED' if total_fail == 0 else str(total_fail) + ' FAILURE(S)'} — summary: {out}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
