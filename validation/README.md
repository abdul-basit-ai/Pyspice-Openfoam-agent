# Validation — physics engine vs TI EVM measurements

This directory validates the pipeline's **physics engine** against real,
published TI evaluation-board measurements. Component selection is
explicitly out of scope: every run injects the EVM's exact parts and
bypasses `select_components` (plan step 3).

## Reproduce every number in `error_report.md`

```bash
# 1. digitize the measured efficiency curve (programmatic: the figure is
#    vector graphics in the PDF; extracted + axis-calibrated, not eyeballed)
python validation/scripts/digitize_efficiency.py

# 2. run the pipeline at every matched load point (deterministic: one run
#    per point is sufficient — the servo is a bounded search, no stochastic
#    component in this path)
python validation/scripts/run_matched_points.py lm27402_evm

# 3. compute the error table + overlay plot
python validation/scripts/compute_error_report.py
```

Requires the Windows-host environment (ngspice + PySpice shared library,
see the repo README). No OpenFOAM needed: thermal validation is out of
scope for the processed EVM (ambient/airflow unstated in the source UG).

## Current EVMs

| EVM | Source | Spec | Result |
|---|---|---|---|
| `lm27402_evm` | TI SNVA406C (LM27402 Buck Controller EVM User's Guide) | 12 V -> 1.5 V @ 300 kHz, 0-20 A | **MAE 0.22 pp over 17 matched loads; worst +0.81 pp (3 A)** |

## Source of every input

- Measured curve: `evms/lm27402_evm/source_pdf/snva406.pdf` (figure 6-1),
  digitized to `digitized_efficiency.csv` with a stated ±0.3 pp
  uncertainty. The 12 V curve is the validation target; the 5 V curve is
  digitized alongside for reference.
- Parts: `bom.yaml` transcribes SNVA406C section 10 verbatim;
  `library_entries.yaml` fills Phase 1 schema fields from each part's own
  datasheet, with a per-field `source` flag: `published`, `derived`
  (derivation shown), `pipeline_estimate` (documented, listed as an
  unmatched factor, sensitivity stated), or `sentinel` (schema-required
  but UNUSED by the simulated physics — screening is bypassed in this
  plan, so no sentinel value can influence any number).
- Conditions: `test_conditions.yaml`, including the explicit
  `unmatched_factors` list that bounds what the error numbers can claim.

## Honest-interpretation notes

- The ±0.3 pp digitization uncertainty is a FLOOR under the reported
  errors; points whose error exceeds 2× uncertainty are flagged in the
  table.
- Over-prediction of efficiency (sim > measured) is the dangerous
  direction and is reported separately from under-prediction.
- The pipeline is deterministic at fixed load (one run per point).
- 19 A is excluded: the digitized measured trace ends at ~19.2 A and the
  tail interpolation is inexact — the exclusion is reported in the error
  table, not silent.

## Sensitivity checks

`inject_fixed_design.py --plateau-scale` (function argument) re-runs any
point with the estimated MOSFET plateau voltage perturbed; results land in
`evms/lm27402_evm/sensitivity_results.csv` and are reported in
`error_report.md`. Rejected/deferred EVM candidates and reasons:
`evms/REJECTED.md`.
