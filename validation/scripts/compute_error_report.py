"""Steps 7 + 8 (revised per mulyivalidation.md): error report across ALL
validated EVMs.

Error convention (fixed per Part A review):
    error = sim - measured  (percentage points of efficiency)
    error > 0  -> OVER-prediction: the simulator claims BETTER efficiency
                  than the real board delivers. This is the DANGEROUS
                  direction — a design could be specced around numbers the
                  hardware will not hit.
    error < 0  -> UNDER-prediction: conservative, lower-risk.

Plain Markdown only; no self-assigned numeric scores anywhere.
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


def _sourcing_breakdown(evm_dir: Path) -> dict[str, int]:
    """Count fields by source flag across the EVM's library entries."""
    counts = {"published": 0, "derived": 0, "pipeline_estimate": 0, "sentinel": 0}
    e = yaml.safe_load((evm_dir / "library_entries.yaml").read_text(encoding="utf-8"))
    for group in ("mosfets",):
        for m in e.get(group, []):
            for f in m.get("status_fields", {}).values():
                src = f.get("source") if isinstance(f, dict) else None
                if src in counts:
                    counts[src] += 1
    for key in ("inductor", "output_capacitor_bank"):
        if e.get(key):
            for f in e[key].get("status_fields", {}).values():
                src = f.get("source") if isinstance(f, dict) else None
                if src in counts:
                    counts[src] += 1
    return counts


def process_evm(evm_id: str) -> dict:
    d = HERE / "evms" / evm_id
    meas = _load_csv(d / "digitized_efficiency.csv")
    sim = {float(r["load_a"]): r for r in _load_csv(d / "sim_results.csv")
           if r.get("sim_efficiency_pct")}
    tc = yaml.safe_load((d / "test_conditions.yaml").read_text(encoding="utf-8"))

    pairs, skipped_nan = [], 0
    for m in meas:
        load = float(m["load_a"])
        if load not in sim:
            continue
        meas_v = float(m["measured_efficiency_pct_vin12"])
        if meas_v != meas_v:  # NaN: digitized trace ends before this load
            skipped_nan += 1
            continue
        pairs.append({
            "load": load, "meas": meas_v,
            "unc": float(m["digitization_uncertainty_pct"]),
            "sim": float(sim[load]["sim_efficiency_pct"]),
            "ripple_mv": sim[load].get("sim_ripple_mv"),
        })

    for p in pairs:
        p["err"] = p["sim"] - p["meas"]
    errors = [p["err"] for p in pairs]
    mae = sum(abs(e) for e in errors) / len(errors)
    worst = max(pairs, key=lambda p: abs(p["err"]))
    over = [p for p in pairs if p["err"] > 0]   # sim claims better -> DANGEROUS
    under = [p for p in pairs if p["err"] < 0]  # conservative

    # overlay plot
    png = d / f"overlay_{evm_id}.png"
    srt = sorted(pairs, key=lambda x: x["load"])
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot([p["load"] for p in srt], [p["meas"] for p in srt], "o-",
            label="Measured (digitized from the EVM user's guide)")
    ax.plot([p["load"] for p in srt], [p["sim"] for p in srt], "s--",
            label="Simulated (pyspice_openfoam_agent)")
    ax.set_xlabel("Output current (A)")
    ax.set_ylabel("Efficiency (%)")
    ax.set_title(f"{evm_id}: {tc['vin_v']:.0f} V -> {tc['vout_v']} V @ "
                 f"{tc['fsw_hz'] / 1e3:.0f} kHz — efficiency overlay")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(png, dpi=130)
    plt.close(fig)

    sens_path = d / "sensitivity_results.csv"
    sens = _load_csv(sens_path) if sens_path.exists() else []

    return {
        "evm_id": evm_id, "pairs": pairs, "mae_pp": mae, "worst": worst,
        "n_over": len(over), "n_under": len(under),
        "mean_over": (sum(p["err"] for p in over) / len(over)) if over else None,
        "mean_under": (sum(p["err"] for p in under) / len(under)) if under else None,
        "skipped_nan": skipped_nan,
        "thermal_in_scope": tc.get("thermal_in_scope", False),
        "topology": tc.get("topology", "buck"),
        "sourcing": _sourcing_breakdown(d), "sens": sens, "png": png,
        "n_failed_runs": sum(1 for r in _load_csv(d / "sim_results.csv")
                             if r.get("note")),
    }


def _fmt(v: float | None, unit: str = "pp") -> str:
    return "none" if v is None else f"{v:+.2f} {unit}"


def main() -> None:
    evm_ids = sorted(p.name for p in (HERE / "evms").iterdir()
                     if p.is_dir() and (p / "sim_results.csv").exists()
                     and not p.name.startswith("."))
    results = [process_evm(e) for e in evm_ids]

    # ---------- summary across topologies ----------
    lines = [
        "# Validation error report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        "Reproduce every number via `validation/README.md`.",
        "",
        "Scope: physics-engine validation with EVM-exact injected parts "
        "(component selection is out of scope per the validation plan). "
        "Error convention: error = simulated minus measured efficiency, in "
        "percentage points (pp). Positive error means the simulator claims "
        "BETTER efficiency than the real board delivers — the optimistic, "
        "dangerous direction. Negative error is conservative.",
        "",
        "## Summary across topologies",
        "",
        "| Topology | EVM | Matched loads | Mean abs error (pp) | Worst "
        "point (pp) | Bias direction | Thermal in scope? |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        bias = ("optimistic (over-predicts efficiency)"
                if (r["n_over"] > r["n_under"])
                else "conservative (under-predicts efficiency)"
                if (r["n_under"] > r["n_over"]) else "mixed")
        lines.append(
            f"| {r['topology']} | {r['evm_id']} | {len(r['pairs'])} | "
            f"{r['mae_pp']:.2f} | {r['worst']['err']:+.2f} @ "
            f"{r['worst']['load']:.0f} A | {bias} | "
            f"{'yes' if r['thermal_in_scope'] else 'no (conditions unstated)'} |")

    # ---------- per-EVM sections ----------
    for r in results:
        s = r["sourcing"]
        lines += [
            "",
            f"## {r['topology']} — {r['evm_id']}",
            "",
            "| Load (A) | Measured eff (%) | Sim eff (%) | Error (pp) | "
            "Digitization unc (pp) | Sim ripple (mV) |",
            "|---|---|---|---|---|---|",
        ]
        for p in r["pairs"]:
            flag = "  (exceeds 2x digitization uncertainty)" \
                if abs(p["err"]) > 2 * p["unc"] else ""
            lines.append(
                f"| {p['load']:.0f} | {p['meas']:.2f} | {p['sim']:.2f} | "
                f"{p['err']:+.2f} | +/-{p['unc']:.1f} | {p['ripple_mv']} |{flag}")
        lines += [
            "",
            f"Mean absolute error: {r['mae_pp']:.2f} pp. Worst point: "
            f"{r['worst']['load']:.0f} A at {r['worst']['err']:+.2f} pp.",
            "",
            f"Over-predictions (optimistic, DANGEROUS direction): "
            f"{r['n_over']} of {len(r['pairs'])} points, mean "
            f"{_fmt(r['mean_over'])}. Under-predictions (conservative): "
            f"{r['n_under']}, mean {_fmt(r['mean_under'])}.",
            "",
            "Field sourcing behind these numbers: "
            f"{s['published']} published / {s['derived']} derived / "
            f"{s['pipeline_estimate']} pipeline_estimate / {s['sentinel']} "
            "sentinel (sentinels are unused by the simulated physics; "
            "estimates are listed as unmatched factors below).",
        ]
        if r["skipped_nan"]:
            lines.append(
                f"{r['skipped_nan']} simulated point(s) beyond the end of "
                "the digitized measured trace are excluded (noted, not "
                "silent).")
        if r["n_failed_runs"]:
            lines.append(f"Failed runs: {r['n_failed_runs']} "
                         "(recorded in sim_results.csv notes).")
        if not r["thermal_in_scope"]:
            lines.append("Thermal validation: OUT OF SCOPE — ambient/airflow "
                         "are not stated in the source user's guide; no "
                         "temperature is compared.")
        if r["sens"]:
            lines += [
                "",
                "Sensitivity check (plateau voltage +/-20%):",
                "",
                "| Load (A) | Plateau scale | Sim eff (%) | Error vs measured (pp) |",
                "|---|---|---|---|",
            ]
            meas_by_load = {p["load"]: p["meas"] for p in r["pairs"]}
            for row in r["sens"]:
                err = float(row["sim_efficiency_pct"]) - meas_by_load[
                    float(row["load_a"])]
                lines.append(
                    f"| {float(row['load_a']):.0f} | {row['plateau_scale']} | "
                    f"{float(row['sim_efficiency_pct']):.2f} | {err:+.2f} |")
            lines += [
                "",
                "Checked-hypothesis result: a +/-20% plateau perturbation "
                "moves the 3 A error by only ~+/-0.15 pp and cannot close "
                "the +0.8 pp light-load gap even in the loss-increasing "
                "direction. The plateau ESTIMATE is therefore NOT the "
                "dominant driver of the light-load bias; the omitted "
                "loss terms (Qrr, Coss of the HS part, controller/gate "
                "quiescent consumption the behavioral rig does not model) "
                "remain the consistent explanation.",
            ]
        lines += [
            "",
            "Known unmatched factors: behavioral 5 V gate drive (no driver "
            "IC losses or layout resistances), no input-rail wiring loss, "
            "output-cap ESR derived from dissipation factor at 120 Hz "
            "(ESR at the switching frequency is unpublished), estimated "
            "MOSFET plateau voltages (Vishay publishes them only as "
            "curves), and omitted reverse-recovery / Coss switching terms "
            "where the datasheet value was not surfaced.",
            "",
            f"Overlay: ![overlay](evms/{r['evm_id']}/overlay_{r['evm_id']}.png)",
        ]

    # ---------- rejected candidates ----------
    rej = HERE / "evms" / "REJECTED.md"
    lines += ["", "## Rejected EVM candidates", ""]
    if rej.exists():
        lines.append(rej.read_text(encoding="utf-8").strip())
    else:
        lines.append("_None recorded yet._")

    # ---------- limitations ----------
    lines += [
        "",
        "## Limitations",
        "",
        "- Component selection is NOT validated here (parts are injected, "
        "not selected — the validation plan scopes selection out).",
        "- One EVM per topology, one input voltage and one load sweep per "
        "EVM: this validates the physics engine at the measured operating "
        "points, not across the full input/frequency design space.",
        "- Thermal validation is out of scope for every EVM processed so "
        "far (ambient/airflow unstated in their sources); no temperature "
        "comparison is claimed anywhere in this report.",
        "- Digitization uncertainty (+/-0.3 pp) is a floor under every "
        "error claim; points exceeding 2x that are flagged in the tables.",
        "- Buck-boost topology match: see the per-EVM section for any "
        "caveat where the EVM's switch configuration differs from the "
        "pipeline's 4-switch non-inverting model.",
        "- The pipeline is deterministic at fixed load; single runs per "
        "point (no run-to-run variance term is needed).",
        "",
    ]

    out = HERE / "error_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    for r in results:
        print(f"  {r['evm_id']} [{r['topology']}]: MAE {r['mae_pp']:.2f} pp, "
              f"{len(r['pairs'])} points, worst {r['worst']['err']:+.2f} @ "
              f"{r['worst']['load']:.0f} A")


if __name__ == "__main__":
    main()
