# Prompt: extend EVM validation to all topologies + fix reporting

You already completed EVM Validation Plan Step 1–8 for the LM27402 EVM (buck,
12 V→1.5 V, 300 kHz) with a strong result (0.22 pp mean absolute efficiency
error across 18 load points). Do two things now: **(A)** fix the reporting
issues found in review of that first result, and **(B)** repeat the same
process for the boost and buck-boost topologies, so all three topologies the
pipeline supports have at least one real-hardware validation point.

Do not touch the pipeline's electrical/thermal engine code as part of this
task — this is a validation and reporting task only.

---

## Part A — Fix the LM27402 result and its report before extending

1. **Error-direction bug:** the current `error_report.md` mislabels which
   direction is "dangerous." Error is defined as `sim − measured`.
   - Positive error (over-prediction) = simulator claims *better* efficiency
     than the real board delivers = the dangerous direction (a design could
     be specced around numbers the hardware won't hit).
   - Negative error (under-prediction) = conservative = lower-risk.
   Recompute the over/under-prediction summary with this convention and
   relabel which one is flagged as the risk. Currently the LM27402 data shows
   13 over-predictions (mean +0.27 pp) vs. 4 under-predictions (mean −0.07 pp)
   — state plainly that the bias skews optimistic, concentrated at 3–6 A.

2. **Regenerate `error_report.md` and the narrative summary cleanly.** The
   previous narrative summary had corrupted/garbled text in several places
   (e.g. mid-word truncations, mangled sentences). Regenerate it as clean
   plain Markdown — no table-rendering artifacts, no truncated words. Verify
   by reading the output file back before reporting completion.

3. **Drop self-assigned numeric ratings** (e.g. "8.5/10", "7.5/10") from any
   report. Report the measured error numbers, the caveats, and the sourcing
   of each estimated field — do not assign an overall subjective score.

4. **Run one sensitivity check** on the light-load over-prediction: the
   LM27402 result attributes the light-load bias (3–6 A) to
   omitted Qrr/Coss terms and an estimated MOSFET plateau voltage (flagged
   ~±20% sensitivity). Re-run the 3 A and 5 A points with the plateau
   voltage estimate perturbed ±20% and report whether the light-load error
   moves proportionally. This turns "we think it's Qrr/Coss" into a checked
   hypothesis — report the result either way, including if it disproves the
   hypothesis.

5. In every report going forward, keep `published` vs. `derived` vs.
   `pipeline_estimate` vs. `sentinel` sourced fields visibly separate in the
   summary, not just in `library_entries.yaml` — the top-level error number
   should be immediately followed by a one-line breakdown of how many inputs
   behind it were not directly from a datasheet.

---

## Part B — Repeat for boost and buck-boost

Follow `validation_plan.md` Steps 1–8 for each topology, with these
topology-specific notes:

### Boost
- Search for a TI (or other major-manufacturer) boost EVM with the same
  completeness bar as before: full schematic, BOM with real part numbers,
  a published efficiency-vs-load curve, and stated ambient/airflow
  conditions.
- If no boost EVM meets the bar with TI parts, broaden the manufacturer
  search (Analog Devices, onsemi, Infineon) before lowering the completeness
  bar itself. Log every board considered and rejected, with a one-line
  reason, in `validation/evms/REJECTED.md` — do not silently settle for an
  incomplete source.

### Buck-boost
- The pipeline's buck-boost is a 4-switch non-inverting topology — look
  specifically for EVMs of that type (not the simpler 2-switch inverting
  buck-boost or SEPIC, which don't match the injected model). If no exact
  4-switch non-inverting match exists with complete test data, pick the
  closest documented 4-switch non-inverting board and record the topology
  match as a caveat rather than skipping validation for this topology
  entirely.

### For both
- Same fixed-injection approach as the buck run (Step 3 of the plan) —
  real BOM parts injected, `select_components` bypassed.
- Same digitization, matched-point run, and error-report process (Steps
  5–7), using the corrected error-direction convention from Part A from the
  start.
- If thermal conditions (ambient + airflow) are unstated for a chosen board,
  thermal validation is out of scope for that topology — say so explicitly,
  don't guess.

---

## Reporting format (applies to all topologies going forward)

Produce a single `validation/error_report.md` covering all EVMs processed
so far, structured as:

```
# Validation error report
Generated: <timestamp> — reproduce via validation/README.md

## Summary across topologies
| Topology | EVM | Mean abs error (pp) | Worst point (pp) | Bias direction | Thermal in scope? |
|---|---|---|---|---|---|
| buck | lm27402_evm | 0.22 | +0.81 @ 3A | optimistic (over-predicts) | no |
| boost | <evm> | ... | ... | ... | ... |
| buck_boost | <evm> | ... | ... | ... | ... |

## <topology> — <evm_id>
<full per-load table, as in the existing LM27402 section>
<bias direction stated correctly per Part A>
<field-sourcing breakdown: N published / N derived / N pipeline_estimate / N sentinel>
<sensitivity-check result if one was run>
<known unmatched factors>
<overlay image link>

## Rejected EVM candidates
<from REJECTED.md, one line each, with reason>

## Limitations
<what this validation does and does not cover — component selection out of
scope, single operating point per topology, thermal out of scope where
airflow/ambient unstated, topology-match caveats for buck-boost if any>
```

No numeric overall score. No narrative "what this does to the rating"
section — the summary table and the per-topology detail are the report.

## Definition of done

- At least 1 EVM validated per topology (buck already done — needs Part A
  fixes only), with the corrected error-direction convention throughout
- `validation/error_report.md` regenerated in the format above, clean text,
  no self-scoring
- `validation/evms/REJECTED.md` lists every board considered and rejected
  per topology, with reasons
- Sensitivity-check result for the buck light-load bias included
- Everything reproducible from `validation/README.md` as before