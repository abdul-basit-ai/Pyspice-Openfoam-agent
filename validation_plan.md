# EVM Validation Plan — pyspice_openfoam_agent

## Objective

Validate the pipeline's electrical and thermal predictions against real, published
TI evaluation-board (EVM) measurements. Output: a reproducible comparison
(overlaid curves + error table) between simulated and measured efficiency,
ripple, and case/board temperature, with all raw inputs and outputs checked
into the repo so the result is independently recomputable.

This plan tests the **physics engine**, not the component-selection logic.
Component selection is validated separately and is explicitly out of scope
here — every run in this plan uses EVM-exact components, injected directly.

---

## Deliverables

```
validation/
├── evms/
│   ├── <evm_id>/
│   │   ├── source_pdf/            # TI User's Guide, saved locally
│   │   ├── bom.yaml                # exact part numbers pulled from the EVM BOM
│   │   ├── library_entries.yaml    # Phase 1 schema entries for those parts
│   │   ├── test_conditions.yaml    # Vin, ambient, airflow, fsw as stated in the report
│   │   ├── digitized_efficiency.csv   # load, measured_efficiency_pct, digitization_uncertainty_pct
│   │   ├── digitized_thermal.csv      # load (or single point), measured_temp_c, location, method
│   │   └── sim_results.csv            # load, sim_efficiency_pct, sim_ripple_mv, sim_temp_c, run_index
│   └── ...
├── scripts/
│   ├── inject_fixed_design.py      # bypasses select_components, forces EVM's exact parts
│   ├── run_matched_points.py       # runs sim at each digitized load point, appends sim_results.csv
│   └── compute_error_report.py     # produces error_report.md + overlay_<evm_id>.png per EVM
├── error_report.md                 # final aggregated output (generated, not hand-written)
└── README.md                       # how to reproduce every number in error_report.md
```

---

## Step 1 — Select 2–3 EVMs

Criteria (all must hold for an EVM to qualify):
- Synchronous buck, single-phase, TI-published
- User's Guide PDF includes: full schematic, BOM with real part numbers,
  efficiency-vs-load curve, and **stated ambient temperature + airflow
  condition** for that curve
- Prefer EVMs where a thermal image or reported case/board temperature at a
  specific load point is also published

Pick boards spanning different power/current levels. Do not proceed past
this step for any EVM missing stated test conditions — log it as rejected
with a one-line reason in `validation/evms/REJECTED.md` instead of skipping
silently.

**Output:** `validation/evms/<evm_id>/source_pdf/` populated, one subfolder
per selected EVM.

---

## Step 2 — Extract BOM and build library entries

For each EVM:
1. Transcribe the exact MOSFET, inductor, and output/input capacitor part
   numbers from the BOM into `bom.yaml`.
2. Pull each part's own datasheet (not a substitute) and fill
   `library_entries.yaml` using the existing Phase 1 schema fields
   (Rds_on, Qg, Qgd, Vplateau, Ciss/Coss/Crss for MOSFETs; inductance, DCR,
   Isat, Irms for the inductor; capacitance, ESR, voltage rating for caps).
3. Any field the datasheet doesn't publish: leave it null and note it in
   `library_entries.yaml` — do not estimate or carry over a default from an
   unrelated part.

**Acceptance check:** every part in `bom.yaml` has a matching, schema-valid
entry in `library_entries.yaml`, or an explicit `status: incomplete` note
naming the missing field.

---

## Step 3 — Build the "fixed design" injection path

Add `validation/scripts/inject_fixed_design.py`:
- Constructs a `Design` object directly (same schema as `design/object.py`)
  with topology, requirements, and the EVM's exact components from
  `library_entries.yaml` — **skips `select_components` entirely**.
- Runs the existing tool chain from `build_netlist` onward: `build_netlist`
  → `run_spice` → `analyze_control_loop` (if applicable) →
  `electro_thermal_converge` → `run_thermal` (only if airflow/ambient are
  both known for that EVM).
- This script should be a thin wrapper around existing orchestrator tools,
  not a reimplementation — if a tool's interface doesn't support a
  fixed-component injection point today, add that as a minimal extension
  point rather than duplicating tool logic.

**Acceptance check:** running this script on any one EVM's
`library_entries.yaml` produces a valid netlist and completes a SPICE run
without modification to core pipeline code.

---

## Step 4 — Set exact test conditions

For each EVM, populate `test_conditions.yaml`:
```yaml
vin_v: <as stated>
vout_v: <as stated>
fsw_hz: <as stated>
ambient_c: <as stated>
airflow: natural_convection | forced_<velocity>_m_s | unknown
notes: <anything the report states that isn't captured above>
unmatched_factors: [connector_loss, trace_resistance, ...]  # things NOT modeled — list explicitly
```
If `airflow: unknown`, thermal validation for that EVM is out of scope —
electrical-only validation proceeds; note this in `error_report.md` rather
than guessing an airflow value.

---

## Step 5 — Digitize measured curves

Use WebPlotDigitizer (open source) on the EVM's published efficiency-vs-load
figure and, where available, the thermal image/table.
- Extract 8–10 points across the load range for efficiency.
- Record digitization uncertainty (typically ±0.3–0.5%) as a column in
  `digitized_efficiency.csv`, not a footnote.
- For thermal: record what was actually measured (case temp via
  thermocouple, board temp via IR image, etc.) in the `method` column of
  `digitized_thermal.csv` — Tj is rarely measured directly; do not relabel
  a case/board temperature as Tj.

**Acceptance check:** `digitized_efficiency.csv` and (if applicable)
`digitized_thermal.csv` exist with uncertainty columns populated, per EVM.

---

## Step 6 — Run the pipeline at each matched point

`validation/scripts/run_matched_points.py`:
- For each row in `digitized_efficiency.csv`, run `inject_fixed_design.py`
  at that exact load (Iout) with `test_conditions.yaml` values.
- Run each point 2–3 times if any part of the pipeline has stochastic
  behavior (optimizer seeds, etc.); if it's fully deterministic, one run is
  sufficient — state which is true for this pipeline in `README.md`.
- Append `load, sim_efficiency_pct, sim_ripple_mv, sim_temp_c, run_index` to
  `sim_results.csv`.

**Acceptance check:** `sim_results.csv` has one row per digitized point
(x run count), with no failed/errored runs — if a run fails, that failure
and its cause go in `error_report.md` as a limitation, not silently dropped.

---

## Step 7 — Compute the error report

`validation/scripts/compute_error_report.py` produces, per EVM:
- Mean absolute % error and worst-point % error, efficiency
- Absolute error (mV), ripple, at matched points
- Signed error (°C), thermal — report over- and under-prediction
  separately; flag any under-prediction explicitly (it's the dangerous
  failure mode for a design tool)
- The pipeline's own run-to-run variance (from the repeated runs in Step 6),
  reported alongside every error number

Output: `error_report.md` (aggregated table across all EVMs) plus one
`overlay_<evm_id>.png` per EVM — digitized measured curve and simulated
curve plotted on the same axes.

---

## Step 8 — Write the final report

`error_report.md` must state, per EVM:
- Every unmatched factor from `test_conditions.yaml`
- Digitization uncertainty as a floor under the reported error
- Whether thermal validation was in scope, and why if not
- A link/reference to the exact commit + raw files used, so the numbers are
  reproducible from the repo alone

---

## Explicit non-goals for this plan

- Does **not** validate component selection logic (parts are injected, not
  selected)
- Does **not** validate topologies other than synchronous buck (no suitable
  TI EVM identified yet for boost/buck-boost with complete test conditions)
- Does **not** claim Tj validation unless the EVM's report gives a
  defensible path from measured temperature to Tj (θJC/θJA + measured case
  temp) — otherwise scope is case/board temperature only

## Definition of done

- 2–3 EVMs fully processed through Steps 1–8
- `error_report.md` and overlay plots generated and committed
- `validation/README.md` lets a third party reproduce every number by
  running the three scripts in order against the checked-in CSVs