# Validation error report

Generated: 2026-09-22 18:58 UTC. Reproduce every number via `validation/README.md`.

Scope: physics-engine validation with EVM-exact injected parts (component selection is out of scope per the validation plan). Error convention: error = simulated minus measured efficiency, in percentage points (pp). Positive error means the simulator claims BETTER efficiency than the real board delivers — the optimistic, dangerous direction. Negative error is conservative.

## Summary across topologies

| Topology | EVM | Matched loads | Mean abs error (pp) | Worst point (pp) | Bias direction | Thermal in scope? |
|---|---|---|---|---|---|---|
| buck | lm27402_evm | 17 | 0.22 | +0.81 @ 3 A | optimistic (over-predicts efficiency) | no (conditions unstated) |

## buck — lm27402_evm

| Load (A) | Measured eff (%) | Sim eff (%) | Error (pp) | Digitization unc (pp) | Sim ripple (mV) |
|---|---|---|---|---|---|
| 2 | 91.29 | 91.20 | -0.09 | +/-0.3 | 7.15 |
| 3 | 92.69 | 93.50 | +0.81 | +/-0.3 | 8.56 |  (exceeds 2x digitization uncertainty)
| 4 | 93.24 | 93.93 | +0.69 | +/-0.3 | 7.24 |  (exceeds 2x digitization uncertainty)
| 5 | 93.40 | 94.00 | +0.60 | +/-0.3 | 7.26 |
| 6 | 93.51 | 93.91 | +0.40 | +/-0.3 | 7.28 |
| 7 | 93.51 | 93.73 | +0.22 | +/-0.3 | 7.3 |
| 8 | 93.35 | 93.50 | +0.15 | +/-0.3 | 7.33 |
| 9 | 93.15 | 93.23 | +0.08 | +/-0.3 | 7.35 |
| 10 | 92.92 | 92.93 | +0.01 | +/-0.3 | 7.37 |
| 11 | 92.54 | 92.62 | +0.08 | +/-0.3 | 7.4 |
| 12 | 92.31 | 92.30 | -0.01 | +/-0.3 | 7.42 |
| 13 | 92.05 | 92.19 | +0.14 | +/-0.3 | 9.77 |
| 14 | 91.70 | 91.84 | +0.14 | +/-0.3 | 9.7 |
| 15 | 91.36 | 91.29 | -0.07 | +/-0.3 | 7.5 |
| 16 | 91.05 | 90.95 | -0.10 | +/-0.3 | 7.52 |
| 17 | 90.73 | 90.79 | +0.06 | +/-0.3 | 9.49 |
| 18 | 90.34 | 90.44 | +0.10 | +/-0.3 | 9.43 |

Mean absolute error: 0.22 pp. Worst point: 3 A at +0.81 pp.

Over-predictions (optimistic, DANGEROUS direction): 13 of 17 points, mean +0.27 pp. Under-predictions (conservative): 4, mean -0.07 pp.

Field sourcing behind these numbers: 21 published / 2 derived / 6 pipeline_estimate / 7 sentinel (sentinels are unused by the simulated physics; estimates are listed as unmatched factors below).
1 simulated point(s) beyond the end of the digitized measured trace are excluded (noted, not silent).
Thermal validation: OUT OF SCOPE — ambient/airflow are not stated in the source user's guide; no temperature is compared.

Sensitivity check (plateau voltage +/-20%):

| Load (A) | Plateau scale | Sim eff (%) | Error vs measured (pp) |
|---|---|---|---|
| 3 | 0.8 | 93.63 | +0.94 |
| 3 | 1.0 | 93.50 | +0.81 |
| 3 | 1.2 | 93.29 | +0.60 |
| 5 | 0.8 | 94.12 | +0.72 |
| 5 | 1.0 | 94.00 | +0.60 |
| 5 | 1.2 | 93.82 | +0.42 |

Checked-hypothesis result: a +/-20% plateau perturbation moves the 3 A error by only ~+/-0.15 pp and cannot close the +0.8 pp light-load gap even in the loss-increasing direction. The plateau ESTIMATE is therefore NOT the dominant driver of the light-load bias; the omitted loss terms (Qrr, Coss of the HS part, controller/gate quiescent consumption the behavioral rig does not model) remain the consistent explanation.

Known unmatched factors: behavioral 5 V gate drive (no driver IC losses or layout resistances), no input-rail wiring loss, output-cap ESR derived from dissipation factor at 120 Hz (ESR at the switching frequency is unpublished), estimated MOSFET plateau voltages (Vishay publishes them only as curves), and omitted reverse-recovery / Coss switching terms where the datasheet value was not surfaced.

Overlay: ![overlay](evms/lm27402_evm/overlay_lm27402_evm.png)

## Rejected EVM candidates

# Rejected / deferred EVM candidates

| Topology | Board / source | Reason |
|---|---|---|
| boost | TI SNVA385 (fetched) | Wrong document: it is the LM3445 LED-driver EVM guide, not the LM5122 boost EVM (search-result literature number was wrong). |
| boost | TI SNVA729A (LM5122EVM-1PH, fetched) | Current rev uses the LMG5200 GaN power stage (integrated half-bridge), not discrete MOSFETs — the rig's injected MOSFET schema (Rds_on/Qg/Qgd/plateau per discrete part) has no defensible mapping. |
| boost | TI LM5122EVM-2PH (SNVA815) | Dual-phase — the pipeline validates single-phase only. |
| buck_boost | TI SLUUBC4 / SLUUBC6 (LM5175EVM user's guides) | URLs 404 at TI's literature server; SNVU439 (LM5175EVM-HD) fetched instead as the selected candidate. |
| buck_boost | TI SNVU439 (LM5175EVM-HD) — **SELECTED, IN PROGRESS** | 4-switch non-inverting (exact topology match), 12 V / 6 A @ 400 kHz, full BOM (QH1/QL1 = BSZ042N06NS, QH2/QL2 = BSZ0902NS, L1 = Wurth 7443551370 3.7 uH/4.9 mOhm, 6x 10 uF 50 V output ceramics), "Efficiency vs. Output Current" figure present and vector-digitizable. BLOCKED ON: verified per-part MOSFET fields for BSZ042N06NS / BSZ0902NS (searches returned only family-typical approximations, which the plan forbids); next action = extract the Infineon datasheet PDFs programmatically as done for the TI catalog, then digitize fig 5 and run the sweep. Sim-model caveat to record: the pipeline drives all four switches with ONE MOSFET part; the EVM uses BSZ042 on one leg and BSZ0902 on the other — validation point VIN = 12 V (transition, all four conduct equally) minimizes the mismatch, which will be stated as a caveat. |
| buck_boost | ADI LTC3780 DC1163 | Quick-start guide (4 pages) lacks stated thermal conditions and a digitizable measured-curve source in the fetched form; deferred as the backup candidate. |

## Limitations

- Component selection is NOT validated here (parts are injected, not selected — the validation plan scopes selection out).
- One EVM per topology, one input voltage and one load sweep per EVM: this validates the physics engine at the measured operating points, not across the full input/frequency design space.
- Thermal validation is out of scope for every EVM processed so far (ambient/airflow unstated in their sources); no temperature comparison is claimed anywhere in this report.
- Digitization uncertainty (+/-0.3 pp) is a floor under every error claim; points exceeding 2x that are flagged in the tables.
- Buck-boost topology match: see the per-EVM section for any caveat where the EVM's switch configuration differs from the pipeline's 4-switch non-inverting model.
- The pipeline is deterministic at fixed load; single runs per point (no run-to-run variance term is needed).
