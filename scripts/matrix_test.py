"""Full coverage matrix: every topology x ripple ratio x switching frequency,
driven through the REAL orchestrator tool chain (size -> select -> screening
-> build -> SPICE steady state). Complements topology_smoke.py (which fixes
one spec per topology) by sweeping the design space the goals' Phase 23
asks for.

Each cell runs: size_converter -> select_components (+screening) ->
build_netlist -> run_spice (real ngspice transient, duty-servoed, measured
ripple/efficiency/health). Results land in runs/matrix_<stamp>/summary.json
and a PASS/FAIL table on stdout.

Usage:
    python scripts/matrix_test.py                 # 3x3x3 = 27 cells
    python scripts/matrix_test.py --quick         # 3 topologies x 2x2 = 12
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.orchestrator.tools import ToolContext, dispatch

# nominal spec per topology (same anchors as topology_smoke.py)
NOMINAL = {
    "buck": dict(Vin=12, Vout=5, Iout=2),
    "boost": dict(Vin=5, Vout=12, Iout=2),
    "buck_boost": dict(Vin=18, Vout=12, Iout=2),
}
RIPPLE_RATIOS = [0.20, 0.30, 0.40]
FREQS_KHZ = [200.0, 350.0, 500.0]
VRIPPLE = {"buck": 0.05, "boost": 0.1, "buck_boost": 0.1}


def run_cell(topo: str, rr: float, fsw_khz: float) -> dict:
    base = NOMINAL[topo]
    ctx = ToolContext(
        run_dir=Path(tempfile.mkdtemp(prefix=f"mx_{topo}_")),
        library=load_library(),
    )
    steps: list[tuple[str, dict]] = []
    r = dispatch(ctx, "size_converter", {
        **base, "fsw_khz": fsw_khz, "Vripple": VRIPPLE[topo],
        "ripple_ratio": rr, "topology": topo,
    })
    steps.append(("size_converter", r.payload))
    if not r.ok:
        return {"cell": f"{topo} rr={rr} fsw={fsw_khz:.0f}k", "pass": False,
                "stage": "size_converter", "error": r.payload.get("error", "?")}
    # the engine may re-classify despite the constraint; report what we got
    if ctx.sizing.topology != topo:
        return {"cell": f"{topo} rr={rr} fsw={fsw_khz:.0f}k", "pass": False,
                "stage": "size_converter",
                "error": f"engine returned {ctx.sizing.topology}, not {topo}"}
    r = dispatch(ctx, "select_components", {})
    steps.append(("select_components", r.payload))
    if not r.ok:
        return {"cell": f"{topo} rr={rr} fsw={fsw_khz:.0f}k", "pass": False,
                "stage": "select_components",
                "rejected": r.payload.get("rejected", False),
                "error": r.payload.get("error", "?")}
    r = dispatch(ctx, "build_netlist", {})
    steps.append(("build_netlist", r.payload))
    if not r.ok:
        return {"cell": f"{topo} rr={rr} fsw={fsw_khz:.0f}k", "pass": False,
                "stage": "build_netlist", "error": r.payload.get("error", "?")}
    r = dispatch(ctx, "run_spice", {})
    steps.append(("run_spice", r.payload))
    if not r.ok:
        return {"cell": f"{topo} rr={rr} fsw={fsw_khz:.0f}k", "pass": False,
                "stage": "run_spice", "error": r.payload.get("error", "?")}
    p = r.payload
    return {
        "cell": f"{topo} rr={rr} fsw={fsw_khz:.0f}k", "pass": True,
        "topology": ctx.sizing.topology,
        "mosfet": ctx.selected.mosfet.part_number,
        "inductor": ctx.selected.inductor.part_number,
        "capacitor": ctx.selected.capacitor.part_number,
        "ripple_mV": p.get("ripple_mV"), "ripple_meets_spec": p.get("ripple_meets_spec"),
        "efficiency": p.get("efficiency"), "duty_trim": p.get("duty_trim"),
        "health_ok": (p.get("health") or {}).get("ok", p.get("health")),
        "cycles": p.get("cycles_to_steady"),
        "warnings": [n for n in (r.payload.get("notes") or [])][:3],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="2x2 grid instead of 3x3")
    args = ap.parse_args()
    ratios = [0.25, 0.40] if args.quick else RIPPLE_RATIOS
    freqs = [250.0, 500.0] if args.quick else FREQS_KHZ

    cells = [(t, rr, f) for t in NOMINAL for rr in ratios for f in freqs]
    print(f"matrix: {len(cells)} cells "
          f"(topologies x ripple {ratios} x fsw {freqs})")
    results = []
    n_pass = 0
    t0 = time.time()
    for i, (topo, rr, fsw) in enumerate(cells, 1):
        try:
            res = run_cell(topo, rr, fsw)
        except Exception as e:  # noqa: BLE001 — a crashed cell is a failed cell
            res = {"cell": f"{topo} rr={rr} fsw={fsw:.0f}k", "pass": False,
                   "stage": "exception", "error": f"{type(e).__name__}: {e}",
                   "traceback": traceback.format_exc()[-800:]}
        results.append(res)
        n_pass += res["pass"]
        mark = "PASS" if res["pass"] else "FAIL"
        extra = (f"eff={res['efficiency']} ripple={res['ripple_mV']}mV"
                 if res["pass"] else f"{res['stage']}: {str(res.get('error'))[:70]}")
        print(f"[{i:02d}/{len(cells)}] {mark} {res['cell']}  {extra}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("runs") / f"matrix_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(
        {"generated": stamp, "n_cells": len(cells), "n_pass": n_pass,
         "wall_s": round(time.time() - t0, 1), "results": results},
        indent=2, default=str), encoding="utf-8")
    print(f"\n{n_pass}/{len(cells)} cells passed "
          f"({time.time() - t0:.0f}s) — summary: {out / 'summary.json'}")
    return 0 if n_pass == len(cells) else 1


if __name__ == "__main__":
    raise SystemExit(main())
