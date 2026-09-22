"""Step 6: run the pipeline at every digitized load point and append
sim_results.csv.

Determinism note (plan step 6): the pipeline is fully deterministic at
fixed load — the servo is a bounded bisection/secant search, the transient
settings are seedless, and no optimizer runs in this path. One run per
point is therefore sufficient; this is stated in validation/README.md.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inject_fixed_design import run_point  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
EVM = HERE / "evms" / "lm27402_evm"


def main() -> int:
    evm = sys.argv[1] if len(sys.argv) > 1 else "lm27402_evm"
    loads = [float(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 \
        else list(range(2, 20))
    out_csv = EVM / "sim_results.csv"
    done: set[float] = set()
    if out_csv.exists():
        with open(out_csv, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                done.add(float(row["load_a"]))

    with open(out_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["load_a", "sim_efficiency_pct", "sim_ripple_mv",
                        "sim_temp_c", "run_index", "note"])
        for load in loads:
            if load in done:
                print(f"[skip] {load} A already in {out_csv.name}", flush=True)
                continue
            t0 = time.time()
            res = run_point(evm, load, Path(tempfile.mkdtemp(prefix=f"val_{evm}_")))
            ok = "error" not in res
            w.writerow([load,
                        res.get("sim_efficiency_pct") if ok else "",
                        res.get("sim_ripple_mv") if ok else "",
                        (res.get("electro_thermal_converge") or {}).get("final_tj_C") if ok else "",
                        1,
                        "" if ok else str(res.get("error"))[:200]])
            f.flush()
            mark = "PASS" if ok else "FAIL"
            extra = (f"eff={res.get('sim_efficiency_pct')}% "
                     f"ripple={res.get('sim_ripple_mv')}mV" if ok
                     else str(res.get("error"))[:90])
            print(f"[{mark}] {load:4.1f} A ({time.time()-t0:.0f}s) {extra}",
                  flush=True)
    print(f"done — {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
