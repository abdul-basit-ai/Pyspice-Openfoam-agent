# Validation error report

Generated: 2026-09-22 17:44 UTC — reproduce via `validation/README.md`.

Scope: **physics-engine validation with EVM-exact injected parts** (selection logic out of scope per plan). Thermal validation is out of scope for every EVM processed so far (ambient/airflow unstated in the source user's guides) — electrical only.

## lm27402_evm

| Load (A) | Measured eff (%) | Sim eff (%) | Error (pp) | Digitization unc (pp) | Sim ripple (mV) |
|---|---|---|---|---|---|
| 2 | 91.29 | 91.20 | -0.09 | ±0.3 | 7.15 |
| 3 | 92.69 | 93.50 | +0.81 | ±0.3 | 8.56 |  <- exceeds digitization uncertainty
| 4 | 93.24 | 93.93 | +0.69 | ±0.3 | 7.24 |  <- exceeds digitization uncertainty
| 5 | 93.40 | 94.00 | +0.60 | ±0.3 | 7.26 |
| 6 | 93.51 | 93.91 | +0.40 | ±0.3 | 7.28 |
| 7 | 93.51 | 93.73 | +0.22 | ±0.3 | 7.3 |
| 8 | 93.35 | 93.50 | +0.15 | ±0.3 | 7.33 |
| 9 | 93.15 | 93.23 | +0.08 | ±0.3 | 7.35 |
| 10 | 92.92 | 92.93 | +0.01 | ±0.3 | 7.37 |
| 11 | 92.54 | 92.62 | +0.08 | ±0.3 | 7.4 |
| 12 | 92.31 | 92.30 | -0.01 | ±0.3 | 7.42 |
| 13 | 92.05 | 92.19 | +0.14 | ±0.3 | 9.77 |
| 14 | 91.70 | 91.84 | +0.14 | ±0.3 | 9.7 |
| 15 | 91.36 | 91.29 | -0.07 | ±0.3 | 7.5 |
| 16 | 91.05 | 90.95 | -0.10 | ±0.3 | 7.52 |
| 17 | 90.73 | 90.79 | +0.06 | ±0.3 | 9.49 |
| 18 | 90.34 | 90.44 | +0.10 | ±0.3 | 9.43 |

- **Mean absolute error: 0.22 pp** (worst point 3 A: +0.81 pp)
- Over-predictions: 13 (mean 0.27 pp); under-predictions: 4 (mean -0.07 pp) — under-prediction is the dangerous direction (claimed better than reality) and is flagged per plan
- Failed/errored runs: 0; 1 sim point(s) beyond the digitized curve end excluded (19 A: measured trace ends ~19.2 A)
- Thermal: out of scope (airflow/ambient unstated in source)
- Overlay: ![overlay](evms/lm27402_evm/overlay_lm27402_evm.png)

Known unmatched factors (see each `test_conditions.yaml`): behavioral 5 V gate drive (no driver IC), no input-rail wiring loss, Kemet output-cap ESR derived from DF@120 Hz (300 kHz ESR unpublished), estimated MOSFET plateau voltages (sensitivity ~±20 % on modeled crossover time), omitted Coss term for SiR436DP and omitted Qrr terms for both FETs (unpublished in surfaced datasheet excerpts).
