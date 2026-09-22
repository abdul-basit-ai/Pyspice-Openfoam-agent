"""Step 7 + 8: compute the per-EVM error table and the overlay plot, and
assemble validation/error_report.md.

Efficiency errors are reported as absolute percentage-point deltas
(sim - measured) at matched loads; the digitization uncertainty is carried
as a floor under every claimed error per the plan.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent


def _load_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def process_evm(evm_id: str) -> dict:
    d = HERE / "evms" / evm_id
    meas = _load_csv(d / "digitized_efficiency.csv")
    sim = {float(r["load_a"]): r for r in _load_csv(d / "sim_results.csv")
           if r.get("sim_efficiency_pct")}
    tc = yaml.safe_load((d / "test_conditions.yaml").read_text(encoding="utf-8"))

    pairs = []
    skipped_nan = 0
    for m in meas:
        load = float(m["load_a"])
        if load in sim:
            meas_v = float(m["measured_efficiency_pct_vin12"])
            if meas_v != meas_v:  # NaN: digitized curve ends (~19.2 A) — excluded
                skipped_nan += 1
                continue
            pairs.append({
                "load": load,
                "meas": meas_v,
                "unc": float(m["digitization_uncertainty_pct"]),
                "sim": float(sim[load]["sim_efficiency_pct"]),
                "ripple_mv": sim[load].get("sim_ripple_mv"),
                "tj_c": sim[load].get("sim_temp_c"),
            })

    errors = [p["sim"] - p["meas"] for p in pairs]
    mae = sum(abs(e) for e in errors) / len(errors)
    worst = max(pairs, key=lambda p: abs(p["sim"] - p["meas"]))
    # over/under-prediction split (under-prediction = sim claims LESS loss
    # margin than reality -> dangerous for a design tool)
    over = [e for e in errors if e > 0]
    under = [e for e in errors if e < 0]

    # overlay plot
    png = d / f"overlay_{evm_id}.png"
    loads_m = [p["load"] for p in sorted(pairs, key=lambda x: x["load"])]
    eff_m = [p["meas"] for p in sorted(pairs, key=lambda x: x["load"])]
    eff_s = [p["sim"] for p in sorted(pairs, key=lambda x: x["load"])]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(loads_m, eff_m, "o-", label="Measured (digitized, SNVA406C fig 6-1)")
    ax.plot(loads_m, eff_s, "s--", label="Simulated (pyspice_openfoam_agent)")
    ax.set_xlabel("Output current (A)")
    ax.set_ylabel("Efficiency (%)")
    ax.set_title(f"{evm_id}: {tc['vin_v']:.0f} V -> {tc['vout_v']} V @ "
                 f"{tc['fsw_hz']/1e3:.0f} kHz — efficiency overlay")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(png, dpi=130)
    plt.close(fig)

    return {
        "evm_id": evm_id, "pairs": pairs, "mae_pp": mae,
        "skipped_nan": skipped_nan,
        "worst": worst, "n_over": len(over), "n_under": len(under),
        "mean_over": (sum(over) / len(over)) if over else None,
        "mean_under": (sum(under) / len(under)) if under else None,
        "thermal_in_scope": tc.get("thermal_in_scope", False),
        "png": png,
        "n_failed_runs": sum(1 for r in _load_csv(d / "sim_results.csv")
                             if r.get("note")),
    }


def format_mean(v: float | None) -> str:
    return "none" if v is None else f"{v:.2f} pp"


def main() -> None:
    evm_ids = [p.name for p in (HERE / "evms").iterdir() if p.is_dir()
               and (p / "sim_results.csv").exists()]
    results = [process_evm(e) for e in sorted(evm_ids)]

    lines = [
        "# Validation error report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — "
        f"reproduce via `validation/README.md`.",
        "",
        "Scope: **physics-engine validation with EVM-exact injected parts** "
        "(selection logic out of scope per plan). Thermal validation is out "
        "of scope for every EVM processed so far (ambient/airflow unstated "
        "in the source user's guides) — electrical only.",
        "",
    ]
    for r in results:
        lines += [
            f"## {r['evm_id']}",
            "",
            "| Load (A) | Measured eff (%) | Sim eff (%) | Error (pp) | "
            "Digitization unc (pp) | Sim ripple (mV) |",
            "|---|---|---|---|---|---|",
        ]
        for p in r["pairs"]:
            err = p["sim"] - p["meas"]
            flag = "  <- exceeds digitization uncertainty" \
                if abs(err) > 2 * p["unc"] else ""
            lines.append(
                f"| {p['load']:.0f} | {p['meas']:.2f} | {p['sim']:.2f} | "
                f"{err:+.2f} | ±{p['unc']:.1f} | {p['ripple_mv']} |{flag}")
        lines += [
            "",
            f"- **Mean absolute error: {r['mae_pp']:.2f} pp** "
            f"(worst point {r['worst']['load']:.0f} A: "
            f"{r['worst']['sim'] - r['worst']['meas']:+.2f} pp)",
            f"- Over-predictions: {r['n_over']} (mean "
            f"{format_mean(r['mean_over'])}); "
            f"under-predictions: {r['n_under']} (mean "
            f"{format_mean(r['mean_under'])})"
            " — under-prediction is the dangerous direction (claimed better "
            "than reality) and is flagged per plan",
            f"- Failed/errored runs: {r['n_failed_runs']}"
            + (f"; {r['skipped_nan']} sim point(s) beyond the digitized curve "
               "end excluded (19 A: measured trace ends ~19.2 A)"
               if r.get("skipped_nan") else ""),
            f"- Thermal: out of scope (airflow/ambient unstated in source)",
            f"- Overlay: ![overlay](evms/{r['evm_id']}/overlay_{r['evm_id']}.png)",
            "",
            "Known unmatched factors (see each `test_conditions.yaml`): "
            "behavioral 5 V gate drive (no driver IC), no input-rail wiring "
            "loss, Kemet output-cap ESR derived from DF@120 Hz (300 kHz ESR "
            "unpublished), estimated MOSFET plateau voltages (sensitivity "
            "~±20 % on modeled crossover time), omitted Coss term for "
            "SiR436DP and omitted Qrr terms for both FETs (unpublished in "
            "surfaced datasheet excerpts).",
            "",
        ]

    out = HERE / "error_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    for r in results:
        print(f"  {r['evm_id']}: MAE {r['mae_pp']:.2f} pp over "
              f"{len(r['pairs'])} matched points")


if __name__ == "__main__":
    main()
